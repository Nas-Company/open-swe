from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from langchain_core.messages.content import create_image_block, create_text_block
from langgraph_sdk import get_client

from agent.dashboard.agent_overrides import resolve_agent_model_id
from agent.dashboard.enabled_repos import is_review_repo_enabled
from agent.dashboard.oauth import build_settings_url
from agent.dashboard.options import model_supports_images
from agent.dashboard.user_mappings import login_for_email, login_for_lark_id
from agent.dashboard.workflow_approval import (
    decide_workflow_push_approval,
    get_workflow_push_approvals,
)
from agent.dispatch import dispatch_agent_run
from agent.utils.github_app import get_github_app_installation_token
from agent.utils.github_org_membership import is_user_active_org_member
from agent.utils.lark import (
    LARK_TENANT_KEY,
    LarkEvent,
    LarkMessage,
    download_lark_image,
    fetch_lark_thread,
    get_lark_user,
    reply_to_lark_message,
)
from agent.utils.lark_events import mark_lark_event_dispatched
from agent.utils.thread_ops import get_thread_active_status, queue_message_for_thread
from agent.webapp import (
    _AGENT_VERSION_METADATA,
    LANGGRAPH_URL,
    _is_repo_allowed,
    generate_thread_id_from_lark,
    upsert_agent_thread_owner_metadata,
)

LARK_GITHUB_ORG = os.environ.get("LARK_GITHUB_ORG", "Nas-Company").strip() or "Nas-Company"
LARK_IMAGE_MAX_COUNT = int(os.environ.get("LARK_IMAGE_MAX_COUNT", "4"))
LARK_IMAGE_MAX_BYTES = int(os.environ.get("LARK_IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))
LARK_IMAGE_TOTAL_MAX_BYTES = int(
    os.environ.get("LARK_IMAGE_TOTAL_MAX_BYTES", str(20 * 1024 * 1024))
)
LARK_APPROVAL_TTL_SECONDS = int(os.environ.get("LARK_APPROVAL_TTL_SECONDS", "900"))

_GITHUB_REPO_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
_lark_approval_locks: dict[tuple[str, str], asyncio.Lock] = {}


@dataclass(frozen=True)
class LarkRepoSelection:
    status: Literal["missing", "selected", "ambiguous"]
    repo: dict[str, str] | None
    repositories: tuple[dict[str, str], ...]


def select_lark_context(
    messages: tuple[LarkMessage, ...] | list[LarkMessage],
    current_message_id: str,
) -> tuple[LarkMessage, ...]:
    selected: list[LarkMessage] = []
    for message in messages:
        if message.message_type in {"text", "post", "image"}:
            selected.append(message)
        if message.message_id == current_message_id:
            break
    return tuple(selected)


def extract_lark_repo_refs(
    messages: tuple[LarkMessage, ...] | list[LarkMessage],
) -> LarkRepoSelection:
    repositories: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        for match in _GITHUB_REPO_URL.finditer(message.text):
            owner = match.group("owner")
            name = match.group("repo").removesuffix(".git")
            key = (owner.lower(), name.lower())
            if key in seen:
                continue
            seen.add(key)
            repositories.append({"owner": owner, "name": name})

    if not repositories:
        return LarkRepoSelection("missing", None, ())
    if len(repositories) > 1:
        return LarkRepoSelection("ambiguous", None, tuple(repositories))
    return LarkRepoSelection("selected", repositories[0], tuple(repositories))


def build_lark_prompt(
    event: LarkEvent,
    messages: tuple[LarkMessage, ...],
    repo: dict[str, str],
    *,
    sender_name: str,
    warnings: tuple[str, ...] = (),
) -> str:
    conversation = (
        "\n".join(f"- {message.text}" for message in messages if message.text.strip())
        or "- (no text; see attached image)"
    )
    warning_section = "\n".join(f"- {warning}" for warning in warnings)
    return (
        "You were invoked from Lark.\n\n"
        f"## Repository\n{repo['owner']}/{repo['name']}\n\n"
        f"## Triggered by\n{sender_name or event.sender.open_id}\n\n"
        "## Lark thread\n"
        f"- Chat ID: {event.message.chat_id}\n"
        f"- Root message ID: {event.message.root_message_id}\n\n"
        f"## Conversation context\n{conversation}\n\n"
        + (f"## Attachment warnings\n{warning_section}\n\n" if warnings else "")
        + "Use `lark_thread_reply` for every clarification, progress update, approval, and final "
        "summary. Use `lark_read_thread_messages` when you need refreshed Lark context. Do not "
        "use Slack or Linear communication tools for this run."
    )


async def process_lark_mention(event: LarkEvent) -> None:
    message = event.message
    thread_id = generate_thread_id_from_lark(
        event.tenant_key,
        message.chat_id,
        message.root_message_id,
    )
    lark_user = await get_lark_user(event.sender.open_id)
    email = _string(lark_user.get("enterprise_email")) or _string(lark_user.get("email"))
    sender_name = _string(lark_user.get("name")) or event.sender.open_id
    github_login = await login_for_lark_id(event.tenant_key, event.sender.open_id)
    if not github_login and email:
        github_login = await login_for_email(email)
    if not github_login:
        settings_url = build_settings_url()
        link = f" ({settings_url})" if settings_url else " in the Open SWE dashboard"
        await _reply_and_finish(
            event,
            f"Connect Lark account{link}, then send this request again.",
            "unmapped_user",
        )
        return

    if not await is_user_active_org_member(github_login, LARK_GITHUB_ORG):
        await _reply_and_finish(
            event,
            f"Your GitHub account `{github_login}` is not an active {LARK_GITHUB_ORG} member.",
            "org_denied",
        )
        return

    thread_messages = await fetch_lark_thread(message.chat_id, message.root_message_id)
    if not any(item.message_id == message.message_id for item in thread_messages):
        thread_messages = (*thread_messages, message)
    context = select_lark_context(thread_messages, message.message_id)
    selection = extract_lark_repo_refs(context)
    if selection.status == "missing":
        await _reply_and_finish(
            event,
            "Please send one GitHub repository or PR link so I know which repository to use.",
            "missing_repo",
        )
        return
    if selection.status == "ambiguous":
        choices = ", ".join(f"`{repo['owner']}/{repo['name']}`" for repo in selection.repositories)
        await _reply_and_finish(
            event,
            f"I found multiple repositories ({choices}). Please send one repository or PR link.",
            "ambiguous_repo",
        )
        return

    repo = selection.repo
    if repo is None:
        raise RuntimeError("selected Lark repository is missing")
    if not _is_repo_allowed(repo):
        await _reply_and_finish(
            event,
            f"`{repo['owner']}/{repo['name']}` is not allowed for Open SWE.",
            "repo_denied",
        )
        return
    if not await is_review_repo_enabled(repo["owner"], repo["name"]):
        await _reply_and_finish(
            event,
            f"`{repo['owner']}/{repo['name']}` is not enabled in Open SWE settings.",
            "repo_disabled",
        )
        return
    app_token = await get_github_app_installation_token(repositories=[repo["name"]])
    if not app_token:
        await _reply_and_finish(
            event,
            f"The Open SWE GitHub App cannot access `{repo['owner']}/{repo['name']}`.",
            "repo_not_installed",
        )
        return

    image_blocks, image_warnings = await _build_lark_image_blocks(context, github_login)
    prompt = build_lark_prompt(
        event,
        context,
        repo,
        sender_name=sender_name,
        warnings=image_warnings,
    )
    content_blocks = [create_text_block(prompt), *image_blocks]
    lark_thread = {
        "tenant_key": event.tenant_key,
        "chat_id": message.chat_id,
        "root_message_id": message.root_message_id,
        "triggering_message_id": message.message_id,
        "triggering_user_open_id": event.sender.open_id,
        "triggering_user_name": sender_name,
    }
    configurable = {
        "repo": repo,
        "lark_thread": lark_thread,
        "user_email": email,
        "source": "lark",
        "github_login": github_login,
    }
    await upsert_agent_thread_owner_metadata(
        thread_id,
        source="lark",
        repo_config=repo,
        github_login=github_login,
        user_email=email or "",
        title=message.text or f"Lark request for {repo['owner']}/{repo['name']}",
        source_context={"lark_thread": lark_thread},
    )

    if await get_thread_active_status(thread_id):
        queued = await queue_message_for_thread(
            thread_id,
            {"source": "lark", "text": prompt, "images": image_blocks},
        )
        if not queued:
            raise RuntimeError("failed to queue Lark follow-up")
        await reply_to_lark_message(
            message.root_message_id,
            {"text": "I added your follow-up to the active Open SWE run."},
        )
        await mark_lark_event_dispatched(event.event_id, "queued")
        return

    await reply_to_lark_message(
        message.root_message_id,
        {"text": f"Working on `{repo['owner']}/{repo['name']}` now."},
    )
    client = get_client(url=LANGGRAPH_URL)
    run = await dispatch_agent_run(
        thread_id,
        content_blocks,
        configurable,
        source="lark",
        metadata=_AGENT_VERSION_METADATA,
        client=client,
    )
    run_id = run.get("run_id") if isinstance(run, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("Lark dispatch did not return a run ID")
    await mark_lark_event_dispatched(event.event_id, run_id)


async def process_lark_card_action(payload: dict[str, object]) -> dict[str, object]:
    header = payload.get("header")
    event = payload.get("event")
    if not isinstance(header, dict) or not isinstance(event, dict):
        return _card_error("Invalid approval payload.")
    operator = event.get("operator")
    action = event.get("action")
    if not isinstance(operator, dict) or not isinstance(action, dict):
        return _card_error("Invalid approval actor or action.")
    value = action.get("value")
    if not isinstance(value, dict):
        return _card_error("Invalid approval value.")

    tenant_key = _string(operator.get("tenant_key"))
    actor_open_id = _string(operator.get("open_id"))
    thread_id = _string(value.get("thread_id"))
    fingerprint = _string(value.get("fingerprint"))
    action_type = _string(value.get("type"))
    decision = _string(value.get("action"))
    if not all((tenant_key, actor_open_id, thread_id, fingerprint, action_type, decision)):
        return _card_error("Approval context is incomplete.")
    if action_type not in {"workflow_push_approval", "plan_approval"}:
        return _card_error("Unknown approval type.")
    if decision not in {"approve", "reject"}:
        return _card_error("Unknown approval decision.")

    client = get_client(url=LANGGRAPH_URL)
    thread = await client.threads.get(thread_id)
    metadata = thread.get("metadata") if isinstance(thread, dict) else None
    if not isinstance(metadata, dict):
        return _card_error("The Open SWE thread no longer exists.")
    source_context = metadata.get("source_context")
    lark_context = source_context.get("lark_thread") if isinstance(source_context, dict) else None
    if not isinstance(lark_context, dict):
        return _card_error("This is not a Lark-owned Open SWE thread.")
    expected_tenant = _string(lark_context.get("tenant_key")) or LARK_TENANT_KEY
    owner_open_id = _string(lark_context.get("triggering_user_open_id"))
    if not expected_tenant or tenant_key != expected_tenant:
        return _card_error("This approval belongs to a different Lark tenant.")
    actor_login = await login_for_lark_id(tenant_key, actor_open_id)
    owner_login = _string(metadata.get("github_login"))
    if (
        not actor_login
        or actor_login != owner_login
        or not owner_open_id
        or actor_open_id != owner_open_id
    ):
        return _card_error("Only the person who requested this run can decide this approval.")

    lock = _lark_approval_locks.setdefault((thread_id, fingerprint), asyncio.Lock())
    async with lock:
        if action_type == "workflow_push_approval":
            return await _process_workflow_card_decision(
                thread_id,
                fingerprint,
                decision,
                actor_open_id,
            )
        return await _process_plan_card_decision(
            client,
            thread_id,
            fingerprint,
            decision,
            actor_open_id,
        )


async def _process_workflow_card_decision(
    thread_id: str,
    fingerprint: str,
    decision: str,
    actor_open_id: str,
) -> dict[str, object]:
    approvals = await get_workflow_push_approvals(thread_id)
    record = approvals.get(fingerprint)
    if not isinstance(record, dict):
        return _card_error("That workflow approval fingerprint is no longer pending.")
    if record.get("status") != "pending":
        return _card_error("That workflow approval was already decided.")
    if _approval_expired(record):
        return _card_error("That workflow approval has expired. Trigger the push again.")

    approved = decision == "approve"
    updated = await decide_workflow_push_approval(
        thread_id,
        fingerprint,
        approved=approved,
        actor=actor_open_id,
    )
    if updated is None:
        return _card_error("That workflow approval no longer exists.")
    if not approved:
        return _card_success("Workflow push rejected. No workflow files will be pushed.")

    queued = await queue_message_for_thread(
        thread_id,
        {
            "source": "lark",
            "text": "Retry the blocked git push now. The exact workflow fingerprint was approved; "
            "do not alter workflow files before pushing.",
        },
    )
    if not queued:
        return _card_error("The approval was saved, but the retry could not be queued.")
    return _card_success("Workflow push approved. Open SWE will retry the exact blocked push.")


async def _process_plan_card_decision(
    client: object,
    thread_id: str,
    fingerprint: str,
    decision: str,
    actor_open_id: str,
) -> dict[str, object]:
    thread = await client.threads.get(thread_id)
    metadata = thread.get("metadata") if isinstance(thread, dict) else None
    if not isinstance(metadata, dict):
        return _card_error("The Open SWE thread no longer exists.")
    raw_approvals = metadata.get("lark_plan_approvals")
    approvals = dict(raw_approvals) if isinstance(raw_approvals, dict) else {}
    record = approvals.get(fingerprint)
    if not isinstance(record, dict):
        return _card_error("That plan approval fingerprint is no longer pending.")
    if record.get("status") != "pending":
        return _card_error("That plan approval was already decided.")
    if _approval_expired(record):
        return _card_error("That plan approval has expired. Ask Open SWE for a fresh plan card.")
    record = dict(record)
    record.update(
        status="approved" if decision == "approve" else "rejected",
        decision=decision,
        actor_open_id=actor_open_id,
        decided_at=datetime.now(UTC).isoformat(),
    )
    approvals[fingerprint] = record
    await client.threads.update(
        thread_id=thread_id,
        metadata={"lark_plan_approvals": approvals, "plan_mode": decision != "approve"},
    )
    instruction = (
        "Proceed with the approved plan and implement it now."
        if decision == "approve"
        else "The plan was rejected. Stay in plan mode and ask what should be revised."
    )
    queued = await queue_message_for_thread(
        thread_id,
        {"source": "lark", "text": instruction},
    )
    if not queued:
        return _card_error("The plan decision was saved, but the follow-up could not be queued.")
    return _card_success(
        "Plan approved; implementation will continue."
        if decision == "approve"
        else "Plan rejected."
    )


def _approval_expired(record: dict[str, object]) -> bool:
    requested_at = _string(record.get("requested_at"))
    if not requested_at:
        return True
    try:
        requested = datetime.fromisoformat(requested_at)
    except ValueError:
        return True
    if requested.tzinfo is None:
        requested = requested.replace(tzinfo=UTC)
    return (datetime.now(UTC) - requested).total_seconds() > LARK_APPROVAL_TTL_SECONDS


def _card_success(message: str) -> dict[str, object]:
    return _card_response("success", message)


def _card_error(message: str) -> dict[str, object]:
    return _card_response("error", message)


def _card_response(toast_type: str, message: str) -> dict[str, object]:
    return {
        "toast": {"type": toast_type, "content": message},
        "card": {
            "type": "raw",
            "data": {
                "schema": "2.0",
                "body": {"elements": [{"tag": "markdown", "content": message}]},
            },
        },
    }


async def _build_lark_image_blocks(
    messages: tuple[LarkMessage, ...],
    github_login: str,
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    attachments = [
        (message.message_id, image_key) for message in messages for image_key in message.image_keys
    ][:LARK_IMAGE_MAX_COUNT]
    if not attachments:
        return [], ()

    model_id = await resolve_agent_model_id(github_login)
    if not model_supports_images(model_id):
        return [], (f"The selected model `{model_id}` does not support images.",)

    blocks: list[dict[str, object]] = []
    warnings: list[str] = []
    total_bytes = 0
    for message_id, image_key in attachments:
        data = await download_lark_image(message_id, image_key)
        if len(data) > LARK_IMAGE_MAX_BYTES:
            warnings.append(f"Image `{image_key}` was too large and was skipped.")
            continue
        if total_bytes + len(data) > LARK_IMAGE_TOTAL_MAX_BYTES:
            warnings.append(
                "The total image size limit was reached; remaining images were skipped."
            )
            break
        mime_type = _image_mime_type(data)
        if mime_type is None:
            warnings.append(f"Image `{image_key}` had an unsupported format and was skipped.")
            continue
        total_bytes += len(data)
        blocks.append(
            create_image_block(base64=base64.b64encode(data).decode("ascii"), mime_type=mime_type)
        )
    return blocks, tuple(warnings)


async def _reply_and_finish(event: LarkEvent, text: str, reason: str) -> None:
    await reply_to_lark_message(event.message.root_message_id, {"text": text})
    await mark_lark_event_dispatched(event.event_id, f"handled:{reason}")


def _image_mime_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
