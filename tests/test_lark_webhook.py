from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent import webapp
from agent.utils import lark, lark_events

APP_ID = "cli-test"
ENCRYPT_KEY = "encrypt-test"
TENANT_KEY = "tenant-a"
VERIFICATION_TOKEN = "verification-test"


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
def configured_lark(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    for name, value in {
        "LARK_APP_ID": APP_ID,
        "LARK_APP_SECRET": "secret-test",
        "LARK_VERIFICATION_TOKEN": VERIFICATION_TOKEN,
        "LARK_ENCRYPT_KEY": ENCRYPT_KEY,
        "LARK_TENANT_KEY": TENANT_KEY,
    }.items():
        monkeypatch.setattr(lark, name, value)
    monkeypatch.setattr(webapp, "LARK_TENANT_KEY", TENANT_KEY, raising=False)
    monkeypatch.setattr(webapp, "get_lark_bot_open_id", AsyncMock(return_value="ou-bot"))
    store = _FakeStore()
    monkeypatch.setattr(lark_events, "_client", lambda: SimpleNamespace(store=store))
    lark_events._event_locks.clear()
    return store


def _event(
    *,
    event_id: str = "evt-1",
    chat_type: str = "group",
    text: str = "@openswe review this",
    mentions: list[dict[str, Any]] | None = None,
    tenant_key: str = TENANT_KEY,
    sender_type: str = "user",
) -> bytes:
    if mentions is None:
        mentions = [
            {
                "key": "@_user_1",
                "id": {"open_id": "ou-bot", "union_id": "on-bot"},
                "name": "openswe",
                "tenant_key": tenant_key,
            }
        ]
    return json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_id": event_id,
                "event_type": "im.message.receive_v1",
                "tenant_key": tenant_key,
                "app_id": APP_ID,
                "token": VERIFICATION_TOKEN,
                "create_time": "1787720163000",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou-user",
                        "union_id": "on-user",
                        "user_id": "user-1",
                    },
                    "sender_type": sender_type,
                    "tenant_key": tenant_key,
                },
                "message": {
                    "message_id": "om-message",
                    "root_id": "om-root",
                    "parent_id": "om-parent",
                    "create_time": "1787720163000",
                    "chat_id": "oc-chat",
                    "chat_type": chat_type,
                    "message_type": "text",
                    "content": json.dumps({"text": text}),
                    "mentions": mentions,
                },
            },
        }
    ).encode()


def _headers(body: bytes, *, signature: str | None = None) -> dict[str, str]:
    timestamp = "1787720163"
    nonce = "nonce-1"
    digest = hashlib.sha256(timestamp.encode() + nonce.encode() + ENCRYPT_KEY.encode() + body)
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature or digest.hexdigest(),
        "Content-Type": "application/json",
    }


async def _post(body: bytes, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webapp.app),
        base_url="http://test",
    ) as client:
        return await client.post("/webhooks/lark", content=body, headers=headers)


@pytest.mark.asyncio
async def test_group_requires_structured_bot_mention(
    configured_lark: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AsyncMock()
    monkeypatch.setattr(webapp, "_process_lark_event", process, raising=False)
    body = _event(text="@openswe hi", mentions=[])

    response = await _post(body, _headers(body))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_without_mention_is_accepted(
    configured_lark: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AsyncMock()
    monkeypatch.setattr(webapp, "_process_lark_event", process, raising=False)
    body = _event(chat_type="p2p", text="check this", mentions=[])

    response = await _post(body, _headers(body))

    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    process.assert_awaited_once()


@pytest.mark.asyncio
async def test_replayed_event_does_not_schedule_twice(
    configured_lark: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AsyncMock()
    monkeypatch.setattr(webapp, "_process_lark_event", process, raising=False)
    body = _event(event_id="evt-replayed")

    first = await _post(body, _headers(body))
    second = await _post(body, _headers(body))

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "duplicate"
    process.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected(configured_lark: _FakeStore) -> None:
    body = _event()

    response = await _post(body, _headers(body, signature="invalid"))

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_tenant_and_bot_authors_are_ignored(
    configured_lark: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AsyncMock()
    monkeypatch.setattr(webapp, "_process_lark_event", process, raising=False)
    wrong_tenant = _event(event_id="evt-other", tenant_key="tenant-b")
    bot_author = _event(event_id="evt-bot", sender_type="app")

    tenant_response = await _post(wrong_tenant, _headers(wrong_tenant))
    bot_response = await _post(bot_author, _headers(bot_author))

    assert tenant_response.json()["status"] == "ignored"
    assert bot_response.json()["status"] == "ignored"
    process.assert_not_awaited()


def test_lark_thread_id_is_stable_and_tenant_scoped() -> None:
    first = webapp.generate_thread_id_from_lark("tenant-a", "oc-chat", "om-root")
    again = webapp.generate_thread_id_from_lark("tenant-a", "oc-chat", "om-root")
    other_tenant = webapp.generate_thread_id_from_lark("tenant-b", "oc-chat", "om-root")

    assert first == again
    assert first != other_tenant


@pytest.mark.asyncio
async def test_url_verification_returns_challenge(configured_lark: _FakeStore) -> None:
    body = json.dumps(
        {
            "challenge": "challenge-code",
            "token": VERIFICATION_TOKEN,
            "type": "url_verification",
        }
    ).encode()

    response = await _post(body, _headers(body))

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-code"}
