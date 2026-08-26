from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent import webapp
from agent.tools.lark_thread_reply import build_lark_approval_card
from agent.utils import lark
from agent.webhooks import lark as lark_webhook

FINGERPRINT = "fp-1"
THREAD_ID = "thread-1"


class _FakeThreads:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self.metadata = metadata
        self.updates: list[dict[str, Any]] = []

    async def get(self, _thread_id: str) -> dict[str, Any]:
        return {"thread_id": THREAD_ID, "status": "busy", "metadata": self.metadata}

    async def update(self, *, thread_id: str, metadata: dict[str, Any]) -> None:
        assert thread_id == THREAD_ID
        self.metadata.update(metadata)
        self.updates.append(metadata)


class _ConflictError(Exception):
    status_code = 409


def _metadata(*, status: str = "pending", requested_at: str | None = None) -> dict[str, Any]:
    return {
        "github_login": "alice",
        "source": "lark",
        "source_context": {
            "lark_thread": {
                "tenant_key": "tenant-a",
                "root_message_id": "om-root",
                "triggering_user_open_id": "ou-owner",
            }
        },
        "workflow_push_approvals": {
            FINGERPRINT: {
                "fingerprint": FINGERPRINT,
                "status": status,
                "requested_at": requested_at or datetime.now(UTC).isoformat(),
            }
        },
    }


def _action(*, actor: str = "ou-owner", action: str = "approve") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "tenant_key": "tenant-a",
            "event_id": "evt-card-1",
            "event_type": "card.action.trigger",
            "token": "verification-test",
        },
        "event": {
            "operator": {"tenant_key": "tenant-a", "open_id": actor},
            "action": {
                "value": {
                    "type": "workflow_push_approval",
                    "action": action,
                    "thread_id": THREAD_ID,
                    "fingerprint": FINGERPRINT,
                }
            },
            "context": {"open_message_id": "om-card", "open_chat_id": "oc-chat"},
        },
    }


def _signed_headers(body: bytes) -> dict[str, str]:
    timestamp = "1787720163"
    nonce = "nonce-card"
    digest = hashlib.sha256(
        timestamp.encode() + nonce.encode() + b"encrypt-test" + body
    ).hexdigest()
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": digest,
    }


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, Any],
) -> tuple[_FakeThreads, AsyncMock]:
    threads = _FakeThreads(metadata)
    client = SimpleNamespace(threads=threads)
    dispatch = AsyncMock(return_value={"run_id": "run-followup"})
    monkeypatch.setattr(lark_webhook, "get_client", lambda **_kwargs: client)
    monkeypatch.setattr(lark_webhook, "login_for_lark_id", AsyncMock(return_value="alice"))
    monkeypatch.setattr(lark_webhook, "dispatch_agent_run", dispatch)
    monkeypatch.setattr(lark_webhook, "_claim_lark_action_once", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lark_webhook,
        "get_workflow_push_approvals",
        AsyncMock(return_value=metadata["workflow_push_approvals"]),
    )
    monkeypatch.setattr(
        lark_webhook,
        "decide_workflow_push_approval",
        _decide(threads),
        raising=False,
    )
    lark_webhook._lark_approval_locks.clear()
    return threads, dispatch


def _decide(threads: _FakeThreads):
    async def decide(
        _thread_id: str,
        fingerprint: str,
        *,
        approved: bool,
        actor: str,
    ) -> dict[str, Any]:
        record = threads.metadata["workflow_push_approvals"][fingerprint]
        record["status"] = "approved" if approved else "rejected"
        record["decided_by"] = actor
        return record

    return decide


def test_card_contains_fingerprint_but_no_secret() -> None:
    card = build_lark_approval_card(
        "Approve workflow push?",
        "workflow_push_approval",
        FINGERPRINT,
        thread_id=THREAD_ID,
    )

    raw = json.dumps(card)
    assert FINGERPRINT in raw
    assert THREAD_ID in raw
    assert "LARK_APP_SECRET" not in raw


def test_signed_card_callback_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark, "LARK_ENCRYPT_KEY", "encrypt-test")
    monkeypatch.setattr(lark, "LARK_VERIFICATION_TOKEN", "verification-test")
    body = json.dumps(_action()).encode()

    payload = lark.verify_lark_card_action(body, _signed_headers(body))

    assert payload["event"]["operator"]["open_id"] == "ou-owner"
    assert payload["event"]["action"]["value"]["fingerprint"] == FINGERPRINT


@pytest.mark.asyncio
async def test_card_route_returns_terminal_card_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "toast": {"type": "success", "content": "Approved"},
        "card": {"type": "raw", "data": {"schema": "2.0", "body": {"elements": []}}},
    }
    monkeypatch.setattr(webapp, "lark_configured", lambda: True)
    monkeypatch.setattr(webapp, "verify_lark_card_action", lambda *_args: _action(), raising=False)
    monkeypatch.setattr(
        webapp,
        "_process_lark_card_action",
        AsyncMock(return_value=expected),
        raising=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webapp.app),
        base_url="http://test",
    ) as client:
        response = await client.post("/webhooks/lark/card", content=b"{}")

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.asyncio
async def test_wrong_user_cannot_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    _, dispatch = _configure(monkeypatch, _metadata())
    monkeypatch.setattr(lark_webhook, "login_for_lark_id", AsyncMock(return_value="bob"))

    result = await lark_webhook.process_lark_card_action(_action(actor="ou-other"))

    assert result["toast"]["type"] == "error"
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_approval_resumes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _, dispatch = _configure(monkeypatch, _metadata())

    first = await lark_webhook.process_lark_card_action(_action())
    replay = await lark_webhook.process_lark_card_action(_action())

    assert first["toast"]["type"] == "success"
    assert replay["toast"]["type"] == "error"
    dispatch.assert_awaited_once()
    assert "Retry the blocked git push" in dispatch.await_args.args[1]


@pytest.mark.asyncio
async def test_expired_approval_never_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    old = datetime.now(UTC) - timedelta(seconds=lark_webhook.LARK_APPROVAL_TTL_SECONDS + 1)
    _, dispatch = _configure(monkeypatch, _metadata(requested_at=old.isoformat()))

    result = await lark_webhook.process_lark_card_action(_action())

    assert result["toast"]["type"] == "error"
    assert "expired" in result["toast"]["content"].lower()
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_fingerprint_mismatch_never_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    _, dispatch = _configure(monkeypatch, _metadata())
    action = _action()
    action["event"]["action"]["value"]["fingerprint"] = "different"

    result = await lark_webhook.process_lark_card_action(action)

    assert result["toast"]["type"] == "error"
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_approval_requires_stored_pending_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, dispatch = _configure(monkeypatch, _metadata())
    action = _action()
    action["event"]["action"]["value"]["type"] = "plan_approval"

    result = await lark_webhook.process_lark_card_action(action)

    assert result["toast"]["type"] == "error"
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_owner_approval_resumes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = _metadata()
    metadata["lark_plan_approvals"] = {
        FINGERPRINT: {
            "fingerprint": FINGERPRINT,
            "status": "pending",
            "requested_at": datetime.now(UTC).isoformat(),
        }
    }
    _, dispatch = _configure(monkeypatch, metadata)
    action = _action()
    action["event"]["action"]["value"]["type"] = "plan_approval"

    first = await lark_webhook.process_lark_card_action(action)
    replay = await lark_webhook.process_lark_card_action(action)

    assert first["toast"]["type"] == "success"
    assert replay["toast"]["type"] == "error"
    dispatch.assert_awaited_once()
    assert metadata["lark_plan_approvals"][FINGERPRINT]["status"] == "approved"


@pytest.mark.asyncio
async def test_workflow_rejection_dispatches_terminal_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, dispatch = _configure(monkeypatch, _metadata())

    result = await lark_webhook.process_lark_card_action(_action(action="reject"))

    assert result["toast"]["type"] == "success"
    dispatch.assert_awaited_once()
    assert "rejected" in dispatch.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_option_action_dispatches_selected_response_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, dispatch = _configure(monkeypatch, _metadata())
    action = _action()
    action["event"]["action"]["value"] = {
        "type": "open_swe_option",
        "thread_id": THREAD_ID,
        "action_id": "option-1",
        "response": "Use repository one",
    }

    result = await lark_webhook.process_lark_card_action(action)

    assert result["toast"]["type"] == "success"
    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[1] == "Use repository one"


@pytest.mark.asyncio
async def test_action_claim_is_atomic_across_processes() -> None:
    create = AsyncMock(side_effect=[{"thread_id": "claim"}, _ConflictError()])
    client = SimpleNamespace(threads=SimpleNamespace(create=create))

    first = await lark_webhook._claim_lark_action_once(client, THREAD_ID, FINGERPRINT)
    replay = await lark_webhook._claim_lark_action_once(client, THREAD_ID, FINGERPRINT)

    assert first is True
    assert replay is False
