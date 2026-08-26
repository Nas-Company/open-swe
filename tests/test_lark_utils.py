from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from agent.utils import lark
from agent.utils.lark import (
    download_lark_image,
    fetch_lark_thread,
    get_lark_user,
    parse_lark_event,
    reply_to_lark_message,
)


def _message_event(
    *,
    chat_type: str = "group",
    message_type: str = "text",
    content: dict[str, str] | None = None,
) -> bytes:
    return json.dumps(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt-1",
                "event_type": "im.message.receive_v1",
                "tenant_key": "tenant-1",
                "app_id": "cli-test",
                "create_time": "1787720163000",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou-user",
                        "union_id": "on-user",
                        "user_id": "user-1",
                    },
                    "sender_type": "user",
                    "tenant_key": "tenant-1",
                },
                "message": {
                    "message_id": "om-message",
                    "root_id": "om-root",
                    "parent_id": "om-parent",
                    "create_time": "1787720163000",
                    "chat_id": "oc-chat",
                    "chat_type": chat_type,
                    "message_type": message_type,
                    "content": json.dumps(content or {"text": "@openswe review this"}),
                    "mentions": [
                        {
                            "key": "@_user_1",
                            "id": {"open_id": "ou-bot", "union_id": "on-bot"},
                            "name": "openswe",
                            "tenant_key": "tenant-1",
                        }
                    ],
                },
            },
        }
    ).encode()


def test_parse_lark_event_normalizes_message() -> None:
    event = parse_lark_event(_message_event())

    assert event.event_id == "evt-1"
    assert event.tenant_key == "tenant-1"
    assert event.sender.open_id == "ou-user"
    assert event.message.root_message_id == "om-root"
    assert event.message.text == "@openswe review this"
    assert event.message.mentions == ("ou-bot",)


def test_parse_lark_image_event_extracts_image_key() -> None:
    event = parse_lark_event(
        _message_event(message_type="image", content={"image_key": "img-v3-test"})
    )

    assert event.message.text == ""
    assert event.message.image_keys == ("img-v3-test",)


def test_parse_lark_rich_post_extracts_text_links_and_images() -> None:
    event = parse_lark_event(
        _message_event(
            message_type="post",
            content={
                "title": "Review request",
                "content": [
                    [
                        {
                            "tag": "a",
                            "text": "PR 44",
                            "href": "https://github.com/Nas-Company/nas-e2e/pull/44",
                        },
                        {"tag": "img", "image_key": "img-post"},
                    ]
                ],
            },
        )
    )

    assert "Review request" in event.message.text
    assert "https://github.com/Nas-Company/nas-e2e/pull/44" in event.message.text
    assert event.message.image_keys == ("img-post",)


def test_parse_lark_event_uses_message_as_root_when_root_is_missing() -> None:
    payload = json.loads(_message_event())
    payload["event"]["message"].pop("root_id")

    event = parse_lark_event(json.dumps(payload).encode())

    assert event.message.root_message_id == "om-message"


def test_lark_configured_requires_every_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LARK_APP_ID",
        "LARK_APP_SECRET",
        "LARK_VERIFICATION_TOKEN",
        "LARK_ENCRYPT_KEY",
        "LARK_TENANT_KEY",
    ):
        monkeypatch.setattr(lark, name, f"configured-{name.lower()}", raising=False)

    assert lark.lark_configured() is True

    monkeypatch.setattr(lark, "LARK_APP_SECRET", "")

    assert lark.lark_configured() is False


def _response(status_code: int, payload: dict[str, object], **headers: str) -> httpx.Response:
    return httpx.Response(status_code, json=payload, headers=headers)


def _transport(responses: list[httpx.Response]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    remaining: Iterator[httpx.Response] = iter(responses)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return next(remaining)

    return httpx.MockTransport(handler), requests


def _configure_api(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    monkeypatch.setattr(lark, "LARK_APP_ID", "cli-test")
    monkeypatch.setattr(lark, "LARK_APP_SECRET", "secret-test")
    monkeypatch.setattr(lark, "_tenant_token", None, raising=False)
    monkeypatch.setattr(
        lark,
        "_http_client",
        lambda: httpx.AsyncClient(transport=transport, base_url="https://open.larksuite.com"),
        raising=False,
    )


@pytest.mark.asyncio
async def test_reply_uses_root_message_and_tenant_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, requests = _transport(
        [
            _response(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            ),
            _response(
                200,
                {"code": 0, "msg": "success", "data": {"message_id": "om-reply"}},
            ),
        ]
    )
    _configure_api(monkeypatch, transport)

    result = await reply_to_lark_message("om-root", {"text": "Working"})

    assert result.ok is True
    assert result.message_id == "om-reply"
    assert requests[-1].url.path == "/open-apis/im/v1/messages/om-root/reply"
    assert requests[-1].headers["authorization"] == "Bearer tenant-token"
    assert json.loads(requests[-1].content) == {
        "msg_type": "text",
        "content": '{"text": "Working"}',
    }


@pytest.mark.asyncio
async def test_rate_limit_honors_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, _ = _transport(
        [
            _response(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            ),
            _response(429, {"code": 99991400, "msg": "rate limited"}, **{"Retry-After": "1"}),
            _response(
                200,
                {"code": 0, "msg": "success", "data": {"message_id": "om-reply"}},
            ),
        ]
    )
    _configure_api(monkeypatch, transport)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(lark.asyncio, "sleep", fake_sleep, raising=False)

    result = await reply_to_lark_message("om-root", {"text": "ok"})

    assert result.ok is True
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_get_lark_user_returns_api_user(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, requests = _transport(
        [
            _response(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            ),
            _response(
                200,
                {
                    "code": 0,
                    "msg": "success",
                    "data": {"user": {"open_id": "ou-user", "name": "Alice"}},
                },
            ),
        ]
    )
    _configure_api(monkeypatch, transport)

    user = await get_lark_user("ou-user")

    assert user == {"open_id": "ou-user", "name": "Alice"}
    assert requests[-1].url.params["user_id_type"] == "open_id"


@pytest.mark.asyncio
async def test_fetch_lark_thread_filters_other_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, _ = _transport(
        [
            _response(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            ),
            _response(
                200,
                {
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "items": [
                            {
                                "message_id": "om-root",
                                "root_id": "",
                                "parent_id": "",
                                "chat_id": "oc-chat",
                                "chat_type": "group",
                                "msg_type": "text",
                                "body": {"content": '{"text":"root"}'},
                            },
                            {
                                "message_id": "om-child",
                                "root_id": "om-root",
                                "parent_id": "om-root",
                                "chat_id": "oc-chat",
                                "chat_type": "group",
                                "msg_type": "text",
                                "body": {"content": '{"text":"child"}'},
                            },
                            {
                                "message_id": "om-other",
                                "root_id": "om-other-root",
                                "parent_id": "",
                                "chat_id": "oc-chat",
                                "chat_type": "group",
                                "msg_type": "text",
                                "body": {"content": '{"text":"other"}'},
                            },
                        ]
                    },
                },
            ),
        ]
    )
    _configure_api(monkeypatch, transport)

    messages = await fetch_lark_thread("oc-chat", "om-root")

    assert [(message.message_id, message.text) for message in messages] == [
        ("om-root", "root"),
        ("om-child", "child"),
    ]


@pytest.mark.asyncio
async def test_fetch_lark_thread_follows_page_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, requests = _transport(
        [
            _response(
                200,
                {
                    "code": 0,
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            ),
            _response(
                200,
                {
                    "code": 0,
                    "data": {"items": [], "has_more": True, "page_token": "next-page"},
                },
            ),
            _response(
                200,
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "message_id": "om-root",
                                "root_id": "",
                                "chat_id": "oc-chat",
                                "chat_type": "group",
                                "msg_type": "text",
                                "body": {"content": '{"text":"root"}'},
                            }
                        ],
                        "has_more": False,
                    },
                },
            ),
        ]
    )
    _configure_api(monkeypatch, transport)

    messages = await fetch_lark_thread("oc-chat", "om-root")

    assert [message.message_id for message in messages] == ["om-root"]
    assert requests[-1].url.params["page_token"] == "next-page"


@pytest.mark.asyncio
async def test_download_lark_image_returns_binary_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, requests = _transport(
        [
            _response(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            ),
            httpx.Response(200, content=b"image-bytes", headers={"content-type": "image/png"}),
        ]
    )
    _configure_api(monkeypatch, transport)

    image = await download_lark_image("om-message", "img-v3-test")

    assert image == b"image-bytes"
    assert requests[-1].url.params["type"] == "image"


@pytest.mark.asyncio
async def test_get_lark_bot_open_id_uses_app_bot_info(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, requests = _transport(
        [
            _response(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "tenant_access_token": "tenant-token",
                    "expire": 7200,
                },
            ),
            _response(
                200,
                {
                    "code": 0,
                    "msg": "success",
                    "bot": {"open_id": "ou-bot", "app_name": "openswe"},
                },
            ),
        ]
    )
    _configure_api(monkeypatch, transport)
    monkeypatch.setattr(lark, "_bot_open_id", None, raising=False)

    open_id = await lark.get_lark_bot_open_id()

    assert open_id == "ou-bot"
    assert requests[-1].url.path == "/open-apis/bot/v3/info"
