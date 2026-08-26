from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages.content import create_image_block, create_text_block
from langgraph_sdk import get_client

from agent.dashboard.agent_overrides import resolve_agent_model_id
from agent.dashboard.enabled_repos import is_review_repo_enabled
from agent.dashboard.oauth import build_settings_url
from agent.dashboard.options import model_supports_images
from agent.dashboard.user_mappings import login_for_email, login_for_lark_id
from agent.dispatch import dispatch_agent_run
from agent.utils.github_app import get_github_app_installation_token
from agent.utils.github_org_membership import is_user_active_org_member
from agent.utils.lark import (
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

_GITHUB_REPO_URL = re.compile(
    r"https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)


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
