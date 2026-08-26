from __future__ import annotations

from typing import Any

from langgraph.config import get_config

from ..utils.lark import LarkApiError, reply_to_lark_message


async def lark_thread_reply(
    message: str,
    options: list[str] | None = None,
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
    if clean_options:
        content = _option_card(message.strip(), clean_options)
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


def _option_card(message: str, options: list[str]) -> dict[str, object]:
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
                            "value": {"type": "open_swe_option", "response": option},
                        }
                        for option in options
                    ],
                },
            ]
        },
    }
