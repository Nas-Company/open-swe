from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.utils import thread_ops


class _Store:
    def __init__(self) -> None:
        self.items: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}

    async def get_item(self, namespace: tuple[str, ...], key: str) -> dict[str, Any] | None:
        value = self.items.get((tuple(namespace), key))
        return {"value": value} if value is not None else None

    async def put_item(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
    ) -> None:
        self.items[(tuple(namespace), key)] = value


@pytest.mark.asyncio
async def test_consumed_queue_receipt_remains_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    store.items[(("queue_receipts", "thread-1"), "evt-1")] = {"consumed": True}
    monkeypatch.setattr(thread_ops, "langgraph_client", lambda: SimpleNamespace(store=store))

    queued = await thread_ops.queue_message_for_thread(
        "thread-1",
        {"text": "duplicate"},
        dedupe_id="evt-1",
    )

    assert queued is True
    assert await thread_ops.queued_message_exists("thread-1", "evt-1") is True
    assert (("queue", "thread-1"), "pending_messages") not in store.items
