from __future__ import annotations

import asyncio
import json
import os
import time
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from agent.utils.http import DEFAULT_HTTP_TIMEOUT

LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
LARK_VERIFICATION_TOKEN = os.environ.get("LARK_VERIFICATION_TOKEN", "")
LARK_ENCRYPT_KEY = os.environ.get("LARK_ENCRYPT_KEY", "")
LARK_TENANT_KEY = os.environ.get("LARK_TENANT_KEY", "")
LARK_API_BASE_URL = os.environ.get("LARK_API_BASE_URL", "https://open.larksuite.com")
LARK_API_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class LarkConfig:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str
    tenant_key: str


@dataclass(frozen=True)
class LarkSender:
    open_id: str
    union_id: str
    user_id: str
    sender_type: str
    tenant_key: str


@dataclass(frozen=True)
class LarkMessage:
    message_id: str
    root_message_id: str
    parent_message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    text: str
    mentions: tuple[str, ...]
    image_keys: tuple[str, ...]
    sender_id: str = ""
    sender_name: str = ""
    sender_type: str = ""


@dataclass(frozen=True)
class LarkEvent:
    event_id: str
    tenant_key: str
    sender: LarkSender
    message: LarkMessage


class LarkApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class LarkApiResult:
    ok: bool
    message_id: str | None = None


@dataclass(frozen=True)
class LarkWebhookResult:
    event: LarkEvent | None
    response: dict[str, Any]


@dataclass(frozen=True)
class _TenantToken:
    value: str
    expires_at: float


_tenant_token: _TenantToken | None = None
_tenant_token_lock = asyncio.Lock()
_bot_open_id: str | None = None
_bot_open_id_lock = asyncio.Lock()


def lark_configured() -> bool:
    return all(
        (
            LARK_APP_ID,
            LARK_APP_SECRET,
            LARK_VERIFICATION_TOKEN,
            LARK_ENCRYPT_KEY,
            LARK_TENANT_KEY,
        )
    )


def parse_lark_event(body: bytes) -> LarkEvent:
    payload = json.loads(body)
    header = payload["header"]
    event_payload = payload["event"]
    sender_payload = event_payload["sender"]
    sender_ids = sender_payload["sender_id"]
    message_payload = event_payload["message"]
    content = _parse_content(message_payload.get("content"))

    message_id = str(message_payload["message_id"])
    message_type = str(message_payload.get("message_type", ""))
    mentions = tuple(
        open_id
        for mention in message_payload.get("mentions", ())
        if (open_id := str(mention.get("id", {}).get("open_id", "")))
    )
    image_keys = _extract_image_keys(content)

    return LarkEvent(
        event_id=str(header["event_id"]),
        tenant_key=str(header["tenant_key"]),
        sender=LarkSender(
            open_id=str(sender_ids.get("open_id", "")),
            union_id=str(sender_ids.get("union_id", "")),
            user_id=str(sender_ids.get("user_id", "")),
            sender_type=str(sender_payload.get("sender_type", "")),
            tenant_key=str(sender_payload.get("tenant_key", "")),
        ),
        message=LarkMessage(
            message_id=message_id,
            root_message_id=str(message_payload.get("root_id") or message_id),
            parent_message_id=str(message_payload.get("parent_id", "")),
            chat_id=str(message_payload["chat_id"]),
            chat_type=str(message_payload.get("chat_type", "")),
            message_type=message_type,
            text=_extract_content_text(content),
            mentions=mentions,
            image_keys=image_keys,
            sender_id=str(sender_ids.get("open_id", "")),
            sender_type=str(sender_payload.get("sender_type", "")),
        ),
    )


def verify_lark_message_event(
    body: bytes,
    headers: Mapping[str, str],
) -> LarkWebhookResult:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"lark_oapi\..*")
        import lark_oapi
        from lark_oapi.core.model import RawRequest

    captured: list[Any] = []
    dispatcher = (
        lark_oapi.EventDispatcherHandler.builder(LARK_ENCRYPT_KEY, LARK_VERIFICATION_TOKEN)
        .register_p2_im_message_receive_v1(captured.append)
        .build()
    )
    raw_request = RawRequest()
    raw_request.uri = "/webhooks/lark"
    raw_request.body = body
    raw_request.headers = {
        "X-Lark-Request-Timestamp": headers.get("X-Lark-Request-Timestamp", ""),
        "X-Lark-Request-Nonce": headers.get("X-Lark-Request-Nonce", ""),
        "X-Lark-Signature": headers.get("X-Lark-Signature", ""),
    }
    sdk_response = dispatcher.do(raw_request)
    response_payload = _parse_content(sdk_response.content)
    if sdk_response.status_code != 200:
        raise LarkApiError("Lark webhook verification failed", code=sdk_response.status_code)
    if not captured:
        return LarkWebhookResult(event=None, response=response_payload)
    normalized_body = lark_oapi.JSON.marshal(captured[0]).encode()
    return LarkWebhookResult(
        event=parse_lark_event(normalized_body),
        response=response_payload,
    )


def verify_lark_card_action(
    body: bytes,
    headers: Mapping[str, str],
) -> dict[str, Any]:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"lark_oapi\..*")
        import lark_oapi
        from lark_oapi.core.model import RawRequest
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

    captured: list[Any] = []

    def capture(event: Any) -> Any:
        captured.append(event)
        return P2CardActionTriggerResponse({"toast": {"type": "success", "content": "Received"}})

    dispatcher = (
        lark_oapi.EventDispatcherHandler.builder(LARK_ENCRYPT_KEY, LARK_VERIFICATION_TOKEN)
        .register_p2_card_action_trigger(capture)
        .build()
    )
    raw_request = RawRequest()
    raw_request.uri = "/webhooks/lark/card"
    raw_request.body = body
    raw_request.headers = {
        "X-Lark-Request-Timestamp": headers.get("X-Lark-Request-Timestamp", ""),
        "X-Lark-Request-Nonce": headers.get("X-Lark-Request-Nonce", ""),
        "X-Lark-Signature": headers.get("X-Lark-Signature", ""),
    }
    sdk_response = dispatcher.do(raw_request)
    if sdk_response.status_code != 200 or not captured:
        raise LarkApiError("Lark card callback verification failed", code=sdk_response.status_code)
    payload = json.loads(lark_oapi.JSON.marshal(captured[0]))
    if not isinstance(payload, dict):
        raise LarkApiError("Lark card callback payload is invalid")
    return payload


async def get_lark_tenant_token(*, force_refresh: bool = False) -> str:
    global _tenant_token

    if not force_refresh and _token_is_fresh(_tenant_token):
        return _tenant_token.value

    async with _tenant_token_lock:
        if not force_refresh and _token_is_fresh(_tenant_token):
            return _tenant_token.value

        async with _http_client() as client:
            response = await client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET},
                timeout=DEFAULT_HTTP_TIMEOUT,
            )
        payload = _json_response(response)
        token = str(payload.get("tenant_access_token", ""))
        if response.status_code >= 400 or payload.get("code") != 0 or not token:
            raise _api_error(response, payload)

        expires_in = max(float(payload.get("expire", 0)), 0.0)
        _tenant_token = _TenantToken(token, time.monotonic() + max(expires_in - 60, 0))
        return token


async def get_lark_user(open_id: str) -> dict[str, Any]:
    payload = await _api_request(
        "GET",
        f"/open-apis/contact/v3/users/{open_id}",
        params={"user_id_type": "open_id"},
    )
    user = payload.get("data", {}).get("user", {})
    return user if isinstance(user, dict) else {}


async def get_lark_bot_open_id() -> str:
    global _bot_open_id

    if _bot_open_id:
        return _bot_open_id
    async with _bot_open_id_lock:
        if _bot_open_id:
            return _bot_open_id
        payload = await _api_request("GET", "/open-apis/bot/v3/info")
        bot = payload.get("bot")
        if not isinstance(bot, dict):
            data = payload.get("data")
            bot = data.get("bot") if isinstance(data, dict) else None
        open_id = bot.get("open_id") if isinstance(bot, dict) else None
        if not isinstance(open_id, str) or not open_id:
            raise LarkApiError("Lark bot info missing open ID")
        _bot_open_id = open_id
        return open_id


async def fetch_lark_thread(chat_id: str, root_message_id: str) -> tuple[LarkMessage, ...]:
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "sort_type": "ByCreateTimeAsc",
        "page_size": "50",
    }
    items: list[dict[str, Any]] = []
    seen_page_tokens: set[str] = set()
    while True:
        payload = await _api_request("GET", "/open-apis/im/v1/messages", params=params)
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        page_items = data.get("items")
        if isinstance(page_items, list):
            items.extend(item for item in page_items if isinstance(item, dict))
        page_token = data.get("page_token")
        if data.get("has_more") is not True or not isinstance(page_token, str) or not page_token:
            break
        if page_token in seen_page_tokens:
            raise LarkApiError("Lark message pagination returned a repeated page token")
        seen_page_tokens.add(page_token)
        params = {**params, "page_token": page_token}
    messages = tuple(_normalize_api_message(item) for item in items if isinstance(item, dict))
    return tuple(
        message
        for message in messages
        if message.message_id == root_message_id or message.root_message_id == root_message_id
    )


async def download_lark_image(message_id: str, image_key: str) -> bytes:
    response = await _authorized_request(
        "GET",
        f"/open-apis/im/v1/messages/{message_id}/resources/{image_key}",
        params={"type": "image"},
    )
    return response.content


async def reply_to_lark_message(
    root_message_id: str,
    content: dict[str, object],
    msg_type: str = "text",
) -> LarkApiResult:
    payload = await _api_request(
        "POST",
        f"/open-apis/im/v1/messages/{root_message_id}/reply",
        json={"msg_type": msg_type, "content": json.dumps(content)},
    )
    message_id = payload.get("data", {}).get("message_id")
    return LarkApiResult(ok=True, message_id=str(message_id) if message_id else None)


async def _api_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = await _authorized_request(method, path, **kwargs)
    payload = _json_response(response)
    if payload.get("code") != 0:
        raise _api_error(response, payload)
    return payload


async def _authorized_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    force_refresh = False
    for attempt in range(LARK_API_MAX_ATTEMPTS):
        token = await get_lark_tenant_token(force_refresh=force_refresh)
        async with _http_client() as client:
            response = await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
                timeout=DEFAULT_HTTP_TIMEOUT,
                **kwargs,
            )

        if response.status_code == 401 and attempt + 1 < LARK_API_MAX_ATTEMPTS:
            force_refresh = True
            continue
        if response.status_code == 429 and attempt + 1 < LARK_API_MAX_ATTEMPTS:
            await asyncio.sleep(_retry_after(response, attempt))
            force_refresh = False
            continue
        if response.status_code >= 500 and attempt + 1 < LARK_API_MAX_ATTEMPTS:
            await asyncio.sleep(float(2**attempt))
            force_refresh = False
            continue
        if response.status_code >= 400:
            raise _api_error(response, _json_response(response))
        return response

    raise LarkApiError("Lark API request exhausted retries")


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=LARK_API_BASE_URL)


def _token_is_fresh(token: _TenantToken | None) -> bool:
    return token is not None and token.expires_at > time.monotonic()


def _json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _api_error(response: httpx.Response, payload: dict[str, Any]) -> LarkApiError:
    code = payload.get("code")
    return LarkApiError(
        str(payload.get("msg") or f"Lark API returned HTTP {response.status_code}"),
        code=code if isinstance(code, int) else None,
        retry_after=_retry_after(response, 0) if response.status_code == 429 else None,
    )


def _retry_after(response: httpx.Response, attempt: int) -> float:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return float(2**attempt)


def _normalize_api_message(payload: dict[str, Any]) -> LarkMessage:
    message_id = str(payload.get("message_id", ""))
    message_type = str(payload.get("msg_type") or payload.get("message_type", ""))
    body = payload.get("body", {})
    content = _parse_content(body.get("content") if isinstance(body, dict) else None)
    sender = payload.get("sender", {})
    mentions = payload.get("mentions")
    return LarkMessage(
        message_id=message_id,
        root_message_id=str(payload.get("root_id") or message_id),
        parent_message_id=str(payload.get("parent_id", "")),
        chat_id=str(payload.get("chat_id", "")),
        chat_type=str(payload.get("chat_type", "")),
        message_type=message_type,
        text=_extract_content_text(content),
        mentions=tuple(
            open_id
            for mention in mentions or ()
            if isinstance(mention, dict)
            and (open_id := str(mention.get("id", {}).get("open_id", "")))
        ),
        image_keys=_extract_image_keys(content),
        sender_id=str(sender.get("id", "")) if isinstance(sender, dict) else "",
        sender_name=str(sender.get("name", "")) if isinstance(sender, dict) else "",
        sender_type=str(sender.get("sender_type", "")) if isinstance(sender, dict) else "",
    )


def _parse_content(raw_content: Any) -> dict[str, Any]:
    if isinstance(raw_content, bytes):
        raw_content = raw_content.decode()
    if isinstance(raw_content, str):
        parsed = json.loads(raw_content)
        return parsed if isinstance(parsed, dict) else {}
    return raw_content if isinstance(raw_content, dict) else {}


def _extract_content_text(content: object) -> str:
    values: list[str] = []

    def walk(value: object, key: str = "") -> None:
        if isinstance(value, str):
            if key in {"text", "title", "href", "url"} and value.strip():
                values.append(value.strip())
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                walk(child, key)

    walk(content)
    return "\n".join(dict.fromkeys(values))


def _extract_image_keys(content: object) -> tuple[str, ...]:
    values: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            image_key = value.get("image_key")
            if isinstance(image_key, str) and image_key:
                values.append(image_key)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(content)
    return tuple(dict.fromkeys(values))
