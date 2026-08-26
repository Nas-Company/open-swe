from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from agent.utils import lark_events
from agent.utils.lark_events import (
    claim_lark_event,
    mark_lark_event_dispatched,
    mark_lark_event_failed,
)


class _FakeStore:
    def __init__(self) -> None:
        self.items: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}

    async def get_item(self, namespace: list[str], key: str) -> dict[str, Any] | None:
        value = self.items.get((tuple(namespace), key))
        return {"value": dict(value)} if value is not None else None

    async def put_item(
        self,
        namespace: list[str],
        key: str,
        value: dict[str, Any],
    ) -> None:
        self.items[(tuple(namespace), key)] = dict(value)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    store = _FakeStore()
    monkeypatch.setattr(lark_events, "_client", lambda: SimpleNamespace(store=store))
    lark_events._event_locks.clear()
    return store


@pytest.mark.asyncio
async def test_first_delivery_claims_and_second_delivery_skips(fake_store: _FakeStore) -> None:
    first = await claim_lark_event("evt-1", "thread-1")
    second = await claim_lark_event("evt-1", "thread-1")

    assert first.status == "claimed"
    assert second.status == "in_progress"
    assert second.record.attempts == 1


@pytest.mark.asyncio
async def test_dispatched_event_is_idempotent(fake_store: _FakeStore) -> None:
    await claim_lark_event("evt-1", "thread-1")
    await mark_lark_event_dispatched("evt-1", "run-1")

    result = await claim_lark_event("evt-1", "thread-1")

    assert result.status == "dispatched"
    assert result.record.run_id == "run-1"


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_owner(fake_store: _FakeStore) -> None:
    results = await asyncio.gather(*[claim_lark_event("evt-1", "thread-1") for _ in range(10)])

    assert [result.status for result in results].count("claimed") == 1
    assert [result.status for result in results].count("in_progress") == 9


@pytest.mark.asyncio
async def test_stale_dispatch_is_reclaimed(
    fake_store: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = datetime(2026, 8, 26, 1, 0, tzinfo=UTC)
    monkeypatch.setattr(lark_events, "_now", lambda: current)
    await claim_lark_event("evt-1", "thread-1")
    current += timedelta(seconds=lark_events.LARK_EVENT_CLAIM_TIMEOUT_SECONDS + 1)

    result = await claim_lark_event("evt-1", "thread-1")

    assert result.status == "claimed"
    assert result.record.attempts == 2


@pytest.mark.asyncio
async def test_failed_event_retries_until_attempt_limit(fake_store: _FakeStore) -> None:
    for attempt in range(lark_events.LARK_EVENT_MAX_ATTEMPTS):
        result = await claim_lark_event("evt-1", "thread-1")
        assert result.status == "claimed"
        assert result.record.attempts == attempt + 1
        await mark_lark_event_failed("evt-1", "dispatch_failed")

    exhausted = await claim_lark_event("evt-1", "thread-1")

    assert exhausted.status == "exhausted"
    assert exhausted.record.error_code == "dispatch_failed"
