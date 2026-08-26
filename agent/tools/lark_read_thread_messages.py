from __future__ import annotations

from typing import Any

from langgraph.config import get_config

from ..utils.lark import fetch_lark_thread, get_lark_user


async def lark_read_thread_messages() -> dict[str, Any]:
    """Read normalized messages from the current Lark thread.

    IDs come exclusively from the run configuration. Use this when the user may have posted a
    follow-up in Lark and the conversation context needs to be refreshed.
    """
    configurable = get_config().get("configurable", {})
    lark_thread = configurable.get("lark_thread", {}) if isinstance(configurable, dict) else {}
    chat_id = lark_thread.get("chat_id") if isinstance(lark_thread, dict) else None
    root_message_id = lark_thread.get("root_message_id") if isinstance(lark_thread, dict) else None
    if not isinstance(chat_id, str) or not chat_id:
        return {"success": False, "error": "Missing lark_thread.chat_id in config"}
    if not isinstance(root_message_id, str) or not root_message_id:
        return {"success": False, "error": "Missing lark_thread.root_message_id in config"}

    messages = await fetch_lark_thread(chat_id, root_message_id)
    names: dict[str, str] = {}
    for sender_id in dict.fromkeys(message.sender_id for message in messages if message.sender_id):
        user = await get_lark_user(sender_id)
        name = user.get("name") if isinstance(user, dict) else None
        names[sender_id] = name if isinstance(name, str) and name else sender_id

    normalized = [
        {
            "author": message.sender_name or names.get(message.sender_id) or "Lark user",
            "text": message.text,
            "message_id": message.message_id,
            "image_keys": list(message.image_keys),
        }
        for message in messages
    ]
    return {"success": True, "messages": normalized, "count": len(normalized)}
