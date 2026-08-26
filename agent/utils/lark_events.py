from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from langgraph_sdk import get_client

LARK_EVENTS_NAMESPACE = ["lark_events"]
LARK_EVENT_CLAIM_TIMEOUT_SECONDS = int(os.environ.get("LARK_EVENT_CLAIM_TIMEOUT_SECONDS", "300"))
LARK_EVENT_MAX_ATTEMPTS = int(os.environ.get("LARK_EVENT_MAX_ATTEMPTS", "3"))

EventStatus = Literal["dispatching", "dispatched", "failed"]
ClaimStatus = Literal["claimed", "in_progress", "dispatched", "exhausted"]


@dataclass(frozen=True)
class LarkEventRecord:
    event_id: str
    status: EventStatus
    thread_id: str
    run_id: str | None
    attempts: int
    claimed_at: str
    updated_at: str
    error_code: str | None


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    record: LarkEventRecord


_event_locks: dict[str, asyncio.Lock] = {}


def _client():
    return get_client()


def _now() -> datetime:
    return datetime.now(UTC)


async def claim_lark_event(event_id: str, thread_id: str) -> ClaimResult:
    lock = _event_locks.setdefault(event_id, asyncio.Lock())
    async with lock:
        record = await _get_record(event_id)
        if record is None:
            claimed = _new_claim(event_id, thread_id, attempts=1)
            if not await _acquire_attempt_claim(event_id, 1):
                return ClaimResult("in_progress", claimed)
            await _put_record(claimed)
            return ClaimResult("claimed", claimed)

        if record.status == "dispatched":
            return ClaimResult("dispatched", record)
        if record.attempts >= LARK_EVENT_MAX_ATTEMPTS:
            return ClaimResult("exhausted", record)
        if record.status == "dispatching" and not _claim_is_stale(record):
            return ClaimResult("in_progress", record)

        next_attempt = record.attempts + 1
        claimed = _new_claim(event_id, thread_id, attempts=next_attempt)
        if not await _acquire_attempt_claim(event_id, next_attempt):
            latest = await _get_record(event_id)
            return ClaimResult("in_progress", latest or claimed)
        await _put_record(claimed)
        return ClaimResult("claimed", claimed)


async def _acquire_attempt_claim(event_id: str, attempt: int) -> bool:
    claim_id = str(uuid5(NAMESPACE_URL, f"open-swe:lark-event:{event_id}:attempt:{attempt}"))
    try:
        await _client().threads.create(
            thread_id=claim_id,
            if_exists="raise",
            metadata={"kind": "lark_internal_claim", "event_id": event_id, "attempt": attempt},
            ttl=10080,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code == 409 or getattr(response, "status_code", None) == 409:
            return False
        raise


async def mark_lark_event_dispatched(event_id: str, run_id: str) -> LarkEventRecord:
    lock = _event_locks.setdefault(event_id, asyncio.Lock())
    async with lock:
        record = await _require_record(event_id)
        updated = LarkEventRecord(
            event_id=record.event_id,
            status="dispatched",
            thread_id=record.thread_id,
            run_id=run_id,
            attempts=record.attempts,
            claimed_at=record.claimed_at,
            updated_at=_now().isoformat(),
            error_code=None,
        )
        await _put_record(updated)
        return updated


async def mark_lark_event_failed(event_id: str, error_code: str) -> LarkEventRecord:
    lock = _event_locks.setdefault(event_id, asyncio.Lock())
    async with lock:
        record = await _require_record(event_id)
        updated = LarkEventRecord(
            event_id=record.event_id,
            status="failed",
            thread_id=record.thread_id,
            run_id=record.run_id,
            attempts=record.attempts,
            claimed_at=record.claimed_at,
            updated_at=_now().isoformat(),
            error_code=error_code,
        )
        await _put_record(updated)
        return updated


def _new_claim(event_id: str, thread_id: str, *, attempts: int) -> LarkEventRecord:
    timestamp = _now().isoformat()
    return LarkEventRecord(
        event_id=event_id,
        status="dispatching",
        thread_id=thread_id,
        run_id=None,
        attempts=attempts,
        claimed_at=timestamp,
        updated_at=timestamp,
        error_code=None,
    )


def _claim_is_stale(record: LarkEventRecord) -> bool:
    claimed_at = datetime.fromisoformat(record.claimed_at)
    return (_now() - claimed_at).total_seconds() > LARK_EVENT_CLAIM_TIMEOUT_SECONDS


async def _require_record(event_id: str) -> LarkEventRecord:
    record = await _get_record(event_id)
    if record is None:
        raise ValueError(f"Lark event {event_id!r} has not been claimed")
    return record


async def _get_record(event_id: str) -> LarkEventRecord | None:
    item = await _client().store.get_item(LARK_EVENTS_NAMESPACE, event_id)
    value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
    if not isinstance(value, dict):
        return None
    return _record_from_value(value)


async def _put_record(record: LarkEventRecord) -> None:
    await _client().store.put_item(LARK_EVENTS_NAMESPACE, record.event_id, asdict(record))


def _record_from_value(value: dict[str, Any]) -> LarkEventRecord:
    return LarkEventRecord(
        event_id=str(value["event_id"]),
        status=value["status"],
        thread_id=str(value["thread_id"]),
        run_id=str(value["run_id"]) if value.get("run_id") else None,
        attempts=int(value["attempts"]),
        claimed_at=str(value["claimed_at"]),
        updated_at=str(value["updated_at"]),
        error_code=str(value["error_code"]) if value.get("error_code") else None,
    )
