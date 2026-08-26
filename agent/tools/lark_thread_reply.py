from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime
from typing import Any

from langgraph.config import get_config
from langgraph_sdk import get_client

from ..utils.lark import LarkApiError, reply_to_lark_message

LANGGRAPH_URL = os.environ.get("LANGGRAPH_URL") or os.environ.get(
    "LANGGRAPH_URL_PROD", "http://localhost:2024"
)
_MAX_PLAN_APPROVALS = 20


async def lark_thread_reply(
    message: str,
    options: list[str] | None = None,
    plan_approval: bool = False,
) -> dict[str, Any]:
    """Reply beneath the current Lark thread root.

    Use this for every clarification, progress update, approval request, and final summary for a
    Lark-triggered run. Pass up to five short options when the user should choose from predefined
    answers. Use Markdown in message text. Never ask the caller for Lark credentials or IDs.
    """
    configurable = get_config().get("configurable", {})
    lark_thread = configurable.get("lark_thread", {}) if isinstance(configurable, dict) else {}
    root_message_id = lark_thread.get("root_message_id") if isinstance(lark_thread, dict) else None
    if not isinstance(root_message_id, str) or not root_message_id:
        return {"success": False, "error": "Missing lark_thread.root_message_id in config"}
    if not message.strip():
        return {"success": False, "error": "Message cannot be empty"}

    clean_options = [option.strip() for option in (options or []) if option.strip()][:5]
    if plan_approval:
        thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            return {"success": False, "error": "Missing thread_id for plan approval"}
        fingerprint = secrets.token_urlsafe(32)
        try:
            await _store_plan_approval(thread_id, fingerprint, message.strip())
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"Could not store plan approval: {exc}"}
        content = build_lark_approval_card(
            message.strip(),
            "plan_approval",
            fingerprint,
            thread_id=thread_id,
        )
        msg_type = "interactive"
    elif clean_options:
        thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            return {"success": False, "error": "Missing thread_id for Lark options"}
        content = _option_card(
            message.strip(),
            clean_options,
            thread_id=thread_id,
            action_id=secrets.token_urlsafe(32),
        )
        msg_type = "interactive"
    else:
        content = {"text": message.strip()}
        msg_type = "text"

    try:
        result = await reply_to_lark_message(
            root_message_id,
            content,
            msg_type=msg_type,
        )
    except LarkApiError as exc:
        return {"success": False, "error": str(exc), "code": exc.code}
    return {"success": result.ok, "message_id": result.message_id}


async def _store_plan_approval(thread_id: str, fingerprint: str, message: str) -> None:
    client = get_client(url=LANGGRAPH_URL)
    thread = await client.threads.get(thread_id)
    metadata = thread.get("metadata") if isinstance(thread, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    raw_approvals = metadata.get("lark_plan_approvals")
    approvals = dict(raw_approvals) if isinstance(raw_approvals, dict) else {}
    approvals[fingerprint] = {
        "fingerprint": fingerprint,
        "status": "pending",
        "requested_at": datetime.now(UTC).isoformat(),
        "message_hash": hashlib.sha256(message.encode()).hexdigest(),
    }
    ordered = sorted(
        approvals.values(),
        key=lambda record: str(record.get("requested_at", "")) if isinstance(record, dict) else "",
    )[-_MAX_PLAN_APPROVALS:]
    await client.threads.update(
        thread_id=thread_id,
        metadata={
            "lark_plan_approvals": {
                str(record["fingerprint"]): record
                for record in ordered
                if isinstance(record, dict) and record.get("fingerprint")
            }
        },
    )


def build_lark_approval_card(
    message: str,
    approval_type: str,
    fingerprint: str,
    *,
    thread_id: str,
) -> dict[str, object]:
    approve_label = (
        "Approve & Implement" if approval_type == "plan_approval" else "Approve workflow push"
    )
    actions = [
        _approval_button(
            approve_label,
            approval_type,
            "approve",
            fingerprint,
            thread_id,
            button_type="primary",
        ),
        _approval_button(
            "Reject",
            approval_type,
            "reject",
            fingerprint,
            thread_id,
            button_type="danger",
        ),
    ]
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": message},
                {"tag": "action", "actions": actions},
            ]
        },
    }


def _approval_button(
    label: str,
    approval_type: str,
    action: str,
    fingerprint: str,
    thread_id: str,
    *,
    button_type: str,
) -> dict[str, object]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "value": {
            "type": approval_type,
            "action": action,
            "fingerprint": fingerprint,
            "thread_id": thread_id,
        },
    }


def _option_card(
    message: str,
    options: list[str],
    *,
    thread_id: str,
    action_id: str,
) -> dict[str, object]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": message},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": option[:75]},
                            "type": "default",
                            "value": {
                                "type": "open_swe_option",
                                "response": option,
                                "thread_id": thread_id,
                                "action_id": action_id,
                            },
                        }
                        for option in options
                    ],
                },
            ]
        },
    }
