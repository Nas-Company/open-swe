from __future__ import annotations

import hashlib
import importlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agent import webapp
from agent.tools import lark_thread_reply
from agent.utils import lark, lark_events
from agent.utils.lark import LarkMessage
from agent.webhooks import lark as lark_webhook

reply_module = importlib.import_module("agent.tools.lark_thread_reply")


class _Store:
    def __init__(self) -> None:
        self.items: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}

    async def get_item(self, namespace: list[str], key: str) -> dict[str, Any] | None:
        value = self.items.get((tuple(namespace), key))
        return {"value": dict(value)} if value is not None else None

    async def put_item(self, namespace: list[str], key: str, value: dict[str, Any]) -> None:
        self.items[(tuple(namespace), key)] = dict(value)


def _event_body() -> bytes:
    return json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt-acceptance",
                "event_type": "im.message.receive_v1",
                "tenant_key": "tenant-a",
                "app_id": "cli-test",
                "token": "verification-test",
                "create_time": "1787720163000",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou-alice", "union_id": "on-alice"},
                    "sender_type": "user",
                    "tenant_key": "tenant-a",
                },
                "message": {
                    "message_id": "om-trigger",
                    "root_id": "om-root",
                    "parent_id": "om-root",
                    "chat_id": "oc-chat",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps(
                        {"text": "@openswe review https://github.com/Nas-Company/nas-e2e/pull/44"}
                    ),
                    "mentions": [
                        {
                            "id": {"open_id": "ou-bot"},
                            "name": "openswe",
                            "key": "@_user_1",
                        }
                    ],
                },
            },
        }
    ).encode()


def _headers(body: bytes) -> dict[str, str]:
    timestamp = "1787720163"
    nonce = "nonce-acceptance"
    signature = hashlib.sha256(
        timestamp.encode() + nonce.encode() + b"encrypt-test" + body
    ).hexdigest()
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature,
    }


@pytest.mark.asyncio
async def test_lark_event_to_agent_to_threaded_reply_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "LARK_APP_ID": "cli-test",
        "LARK_APP_SECRET": "secret-test",
        "LARK_VERIFICATION_TOKEN": "verification-test",
        "LARK_ENCRYPT_KEY": "encrypt-test",
        "LARK_TENANT_KEY": "tenant-a",
    }.items():
        monkeypatch.setattr(lark, name, value)
    monkeypatch.setattr(webapp, "LARK_TENANT_KEY", "tenant-a")
    monkeypatch.setattr(webapp, "get_lark_bot_open_id", AsyncMock(return_value="ou-bot"))
    store = _Store()
    monkeypatch.setattr(lark_events, "_client", lambda: SimpleNamespace(store=store))
    monkeypatch.setattr(
        lark_events,
        "_acquire_attempt_claim",
        AsyncMock(return_value=True),
    )
    lark_events._event_locks.clear()

    trigger = lark.parse_lark_event(_event_body()).message
    image = LarkMessage(
        message_id="om-image",
        root_message_id="om-root",
        parent_message_id="om-root",
        chat_id="oc-chat",
        chat_type="group",
        message_type="image",
        text="",
        mentions=(),
        image_keys=("img-1",),
    )
    monkeypatch.setattr(lark_webhook, "fetch_lark_thread", AsyncMock(return_value=(image, trigger)))
    monkeypatch.setattr(
        lark_webhook,
        "get_lark_user",
        AsyncMock(return_value={"name": "Alice", "email": "alice@nas.io"}),
    )
    monkeypatch.setattr(lark_webhook, "login_for_lark_id", AsyncMock(return_value="alice"))
    monkeypatch.setattr(lark_webhook, "is_user_active_org_member", AsyncMock(return_value=True))
    monkeypatch.setattr(lark_webhook, "_is_repo_allowed", lambda _repo: True)
    monkeypatch.setattr(lark_webhook, "is_review_repo_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lark_webhook, "get_github_app_installation_token", AsyncMock(return_value="app-token")
    )
    monkeypatch.setattr(
        lark_webhook,
        "resolve_agent_model_id",
        AsyncMock(return_value="anthropic:claude-sonnet-4-5"),
    )
    monkeypatch.setattr(lark_webhook, "model_supports_images", lambda _model: True)
    monkeypatch.setattr(
        lark_webhook,
        "download_lark_image",
        AsyncMock(return_value=b"\x89PNG\r\n\x1a\nimage"),
    )
    monkeypatch.setattr(lark_webhook, "upsert_agent_thread_owner_metadata", AsyncMock())
    monkeypatch.setattr(lark_webhook, "get_client", lambda **_kwargs: SimpleNamespace())
    ingress_replies = AsyncMock(return_value=MagicMock(ok=True, message_id="om-working"))
    monkeypatch.setattr(lark_webhook, "reply_to_lark_message", ingress_replies)
    runs: list[dict[str, Any]] = []

    async def dispatch(
        thread_id: str,
        content: list[dict[str, Any]],
        configurable: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, str]:
        runs.append({"thread_id": thread_id, "content": content, "configurable": configurable})
        return {"run_id": "run-1"}

    monkeypatch.setattr(lark_webhook, "dispatch_agent_run", dispatch)

    body = _event_body()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webapp.app), base_url="http://test"
    ) as client:
        first = await client.post("/webhooks/lark", content=body, headers=_headers(body))
        replay = await client.post("/webhooks/lark", content=body, headers=_headers(body))

    assert first.json()["status"] == "accepted"
    assert replay.json()["status"] == "duplicate"
    assert len(runs) == 1
    assert any(block.get("type") == "image" for block in runs[0]["content"])

    agent_reply = AsyncMock(return_value=MagicMock(ok=True, message_id="om-final"))
    monkeypatch.setattr(
        reply_module,
        "get_config",
        lambda: {
            "configurable": {
                "thread_id": runs[0]["thread_id"],
                "lark_thread": {"root_message_id": "om-root"},
            }
        },
    )
    monkeypatch.setattr(reply_module, "reply_to_lark_message", agent_reply)

    result = await lark_thread_reply("PR opened: https://github.com/Nas-Company/nas-e2e/pull/45")

    assert result["success"] is True
    assert agent_reply.await_args.args[0] == "om-root"


@pytest.mark.asyncio
async def test_health_reports_lark_configuration_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webapp, "lark_configured", lambda: True)

    result = await webapp.health_check()

    assert result == {"status": "healthy", "lark_configured": True}
    assert "secret" not in json.dumps(result).lower()
