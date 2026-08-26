from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from agent.utils.http import DEFAULT_HTTP_TIMEOUT

LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
LARK_TENANT_KEY = os.environ.get("LARK_TENANT_KEY", "")

LARK_STATE_COOKIE_NAME = "osw_lark_oauth_state"
LARK_OAUTH_SCOPES = "contact:user.base:readonly"

_AUTHORIZE_URL = "https://accounts.larksuite.com/open-apis/authen/v1/authorize"
_TOKEN_URL = "https://open.larksuite.com/open-apis/authen/v2/oauth/token"
_USERINFO_URL = "https://open.larksuite.com/open-apis/authen/v1/user_info"


@dataclass(frozen=True)
class LarkIdentity:
    open_id: str
    union_id: str
    user_id: str
    tenant_key: str
    email: str | None
    name: str | None


def lark_oauth_configured() -> bool:
    return bool(LARK_APP_ID and LARK_APP_SECRET and LARK_TENANT_KEY)


def build_lark_authorize_url(*, redirect_uri: str, state: str) -> str:
    return f"{_AUTHORIZE_URL}?{urlencode(_authorize_params(redirect_uri, state))}"


async def exchange_lark_code(code: str, redirect_uri: str) -> str:
    async with _http_client() as client:
        response = await client.post(
            _TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": LARK_APP_ID,
                "client_secret": LARK_APP_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    payload = _response_payload(response, "Lark OAuth exchange")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise HTTPException(400, "Lark OAuth exchange missing access token")
    return access_token


async def fetch_lark_identity(access_token: str) -> LarkIdentity:
    async with _http_client() as client:
        response = await client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    payload = _response_payload(response, "Lark user info")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(400, "Lark user info missing identity")
    open_id = data.get("open_id")
    tenant_key = data.get("tenant_key")
    if not isinstance(open_id, str) or not open_id:
        raise HTTPException(400, "Lark user info missing open ID")
    if not isinstance(tenant_key, str) or not tenant_key:
        raise HTTPException(400, "Lark user info missing tenant")
    return LarkIdentity(
        open_id=open_id,
        union_id=_optional_string(data.get("union_id")) or "",
        user_id=_optional_string(data.get("user_id")) or "",
        tenant_key=tenant_key,
        email=_optional_string(data.get("enterprise_email")) or _optional_string(data.get("email")),
        name=_optional_string(data.get("name")),
    )


def verify_lark_tenant(identity: LarkIdentity) -> None:
    if not LARK_TENANT_KEY or identity.tenant_key != LARK_TENANT_KEY:
        raise HTTPException(403, "Lark account is not in the authorized tenant")


def _authorize_params(redirect_uri: str, state: str) -> dict[str, str]:
    return {
        "app_id": LARK_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": LARK_OAUTH_SCOPES,
        "state": state,
    }


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT)


def _response_payload(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(502, f"{operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(502, f"{operation} returned invalid data")
    if response.status_code >= 400 or payload.get("code", 0) != 0:
        detail = payload.get("msg") or payload.get("message") or "unknown error"
        raise HTTPException(400, f"{operation} failed: {detail}")
    return payload


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
