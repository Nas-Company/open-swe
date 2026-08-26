from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from agent.dashboard import lark_oauth, routes
from agent.dashboard.lark_oauth import LarkIdentity


def test_build_authorize_url_has_state_and_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_oauth, "LARK_APP_ID", "cli-test")

    url = lark_oauth.build_lark_authorize_url(
        redirect_uri="https://api.example/lark/callback",
        state="nonce",
    )

    query = parse_qs(urlparse(url).query)
    assert query["app_id"] == ["cli-test"]
    assert query["redirect_uri"] == ["https://api.example/lark/callback"]
    assert query["state"] == ["nonce"]
    assert query["response_type"] == ["code"]


def test_lark_oauth_configured_requires_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_oauth, "LARK_APP_ID", "cli-test")
    monkeypatch.setattr(lark_oauth, "LARK_APP_SECRET", "secret")
    monkeypatch.setattr(lark_oauth, "LARK_TENANT_KEY", "tenant-a")
    assert lark_oauth.lark_oauth_configured() is True

    monkeypatch.setattr(lark_oauth, "LARK_TENANT_KEY", "")
    assert lark_oauth.lark_oauth_configured() is False


def test_verify_lark_tenant_rejects_other_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lark_oauth, "LARK_TENANT_KEY", "tenant-a")
    identity = LarkIdentity(
        open_id="ou-1",
        union_id="on-1",
        user_id="user-1",
        tenant_key="tenant-b",
        email=None,
        name=None,
    )

    with pytest.raises(HTTPException) as exc:
        lark_oauth.verify_lark_tenant(identity)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_exchange_and_fetch_identity_use_verified_lark_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"code": 0, "access_token": "user-token"})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "open_id": "ou-1",
                    "union_id": "on-1",
                    "user_id": "user-1",
                    "tenant_key": "tenant-a",
                    "email": "lark-user@nas.io",
                    "name": "Alice",
                },
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(lark_oauth, "LARK_APP_ID", "cli-test")
    monkeypatch.setattr(lark_oauth, "LARK_APP_SECRET", "secret")
    monkeypatch.setattr(
        lark_oauth,
        "_http_client",
        lambda: httpx.AsyncClient(transport=transport),
        raising=False,
    )

    token = await lark_oauth.exchange_lark_code(
        "code-1",
        "https://api.example/dashboard/api/lark/callback",
    )
    identity = await lark_oauth.fetch_lark_identity(token)

    assert token == "user-token"
    assert identity.open_id == "ou-1"
    assert identity.tenant_key == "tenant-a"
    assert requests[-1].headers["authorization"] == "Bearer user-token"


@pytest.mark.asyncio
async def test_callback_maps_verified_lark_identity_to_logged_in_github_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://dashboard.example")
    monkeypatch.setenv("DASHBOARD_API_BASE_URL", "https://api.example")
    monkeypatch.setattr(
        routes,
        "decode_state",
        lambda _state: {
            "nonce_hash": "hashed-nonce",
            "redirect_to": "https://dashboard.example/my-settings",
        },
    )
    monkeypatch.setattr(routes, "hash_state_nonce", lambda _nonce: "hashed-nonce")
    monkeypatch.setattr(routes, "exchange_lark_code", _async_return("user-token"))
    monkeypatch.setattr(
        routes,
        "fetch_lark_identity",
        _async_return(
            LarkIdentity(
                open_id="ou-alice",
                union_id="on-alice",
                user_id="alice",
                tenant_key="tenant-a",
                email="different-lark-email@nas.io",
                name="Alice",
            )
        ),
    )
    monkeypatch.setattr(routes, "verify_lark_tenant", lambda _identity: None)

    async def capture_mapping(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(routes, "upsert_mapping", capture_mapping)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/dashboard/api/lark/callback",
            "query_string": b"",
            "headers": [(b"cookie", b"osw_lark_oauth_state=nonce")],
            "client": ("test", 123),
            "server": ("api.example", 443),
        }
    )

    response = await routes.lark_callback(
        request,
        code="code-1",
        state="state-1",
        session={"sub": "jimbo23", "email": "kiefersoon@gmail.com"},
    )

    assert response.status_code == 302
    assert captured == {
        "github_login": "jimbo23",
        "work_email": "kiefersoon@gmail.com",
        "lark_tenant_key": "tenant-a",
        "lark_open_id": "ou-alice",
        "lark_union_id": "on-alice",
        "lark_display_name": "Alice",
        "source": "lark_oauth",
        "status": "active",
    }


def _async_return(value: object):
    async def result(*_args: object, **_kwargs: object) -> object:
        return value

    return result
