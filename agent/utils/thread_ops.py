"""Shared LangGraph thread helpers for the dashboard.

The webhook triggers (Slack / Linear / GitHub) dispatch through
``agent.dispatch.dispatch_agent_run`` with ``multitask_strategy="interrupt"``,
so they no longer need a busy-check or an in-process lock. The store-queue
below is retained for the dashboard's deliberate "inject a follow-up into a
run that's already in flight" path (``thread_api.send_dashboard_message``).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langgraph_sdk import get_client

logger = logging.getLogger(__name__)

MAX_QUEUED_MESSAGES = 100
QUEUE_RECEIPT_TTL_MINUTES = 10080


def langgraph_url() -> str:
    return os.environ.get("LANGGRAPH_URL") or os.environ.get(
        "LANGGRAPH_URL_PROD", "http://localhost:2024"
    )


def langgraph_client():
    return get_client(url=langgraph_url())


async def get_thread_active_status(thread_id: str) -> bool | None:
    """Return whether the thread is active, or None when status cannot be determined."""
    try:
        thread = await langgraph_client().threads.get(thread_id)
        status = thread.get("status", "idle") if isinstance(thread, dict) else "idle"
        logger.info("Thread %s status check: status=%s", thread_id, status)
        return status == "busy"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to get thread status for %s: %s", thread_id, exc)
        return None


async def queue_message_for_thread(
    thread_id: str,
    message_content: str | list[dict[str, Any]] | dict[str, Any],
    *,
    dedupe_id: str | None = None,
) -> bool:
    """Queue a follow-up message for a busy thread (FIFO store namespace).

    Used by the dashboard to inject a follow-up into a run that's already in
    flight; webhook triggers use ``multitask_strategy="interrupt"`` instead.
    """
    client = langgraph_client()
    try:
        namespace = ("queue", thread_id)
        key = "pending_messages"
        new_message = {"content": message_content}
        if dedupe_id:
            receipt = await client.store.get_item(("queue_receipts", thread_id), dedupe_id)
            if receipt is not None:
                return True
            new_message["dedupe_id"] = dedupe_id

        existing_messages: list[dict[str, Any]] = []
        try:
            existing_item = await client.store.get_item(namespace, key)
            if existing_item and existing_item.get("value"):
                existing_messages = existing_item["value"].get("messages", [])
        except Exception:  # noqa: BLE001
            logger.debug("No existing queued messages for thread %s", thread_id)

        if dedupe_id and any(item.get("dedupe_id") == dedupe_id for item in existing_messages):
            return True
        existing_messages.append(new_message)
        if len(existing_messages) > MAX_QUEUED_MESSAGES:
            existing_messages = existing_messages[-MAX_QUEUED_MESSAGES:]
            logger.warning(
                "Thread %s queue capped at %d messages (dropped oldest)",
                thread_id,
                MAX_QUEUED_MESSAGES,
            )
        await client.store.put_item(namespace, key, {"messages": existing_messages})
        logger.info(
            "Queued message for thread %s (total queued: %d)",
            thread_id,
            len(existing_messages),
        )
        return True
    except Exception:
        logger.exception("Failed to queue message for thread %s", thread_id)
        return False


async def queued_message_exists(thread_id: str, dedupe_id: str) -> bool:
    if not dedupe_id:
        return False
    try:
        client = langgraph_client()
        receipt = await client.store.get_item(("queue_receipts", thread_id), dedupe_id)
        if receipt is not None:
            return True
        item = await client.store.get_item(("queue", thread_id), "pending_messages")
    except Exception:  # noqa: BLE001
        return False
    value = item.get("value") if isinstance(item, dict) else None
    messages = value.get("messages") if isinstance(value, dict) else None
    return isinstance(messages, list) and any(
        isinstance(message, dict) and message.get("dedupe_id") == dedupe_id for message in messages
    )
