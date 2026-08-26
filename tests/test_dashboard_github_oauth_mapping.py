from __future__ import annotations

import pytest
from starlette.requests import Request

from agent.dashboard import oauth, routes


@pytest.mark.asyncio
async def test_auth_callback_maps_github_login_to_oauth_email(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("DASHBOARD_BASE_URL", "http://localhost:3000")

    monkeypatch.setattr(
        routes,
        "decode_state",
        lambda state: {"nonce_hash": "hashed-nonce", "redirect_to": "http://localhost:3000"},
    )
    monkeypatch.setattr(routes, "hash_state_nonce", lambda nonce: "hashed-nonce")
    monkeypatch.setattr(
        routes,
        "exchange_code",
        lambda code: _async_result({"access_token": "user-token"}),
    )
    monkeypatch.setattr(
        routes,
        "fetch_github_user",
        lambda token: _async_result(
            ({"login": "jimbo23", "avatar_url": "https://example.com/avatar"}, "techshare@nas.io")
        ),
    )
    monkeypatch.setattr(routes, "enforce_org_login_gate", lambda login: _async_result(None))
    monkeypatch.setattr(
        routes,
        "upsert_access_token_from_github_response",
        lambda login, email, token_data: _capture_async(
            captured, "oauth", (login, email, token_data)
        ),
    )
    monkeypatch.setattr(
        routes,
        "upsert_mapping",
        lambda **kwargs: _capture_async(captured, "mapping", kwargs),
    )
    monkeypatch.setattr(routes, "issue_session", lambda **kwargs: "session-token")

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/dashboard/api/auth/callback",
            "query_string": b"",
            "headers": [(b"cookie", f"{routes.STATE_COOKIE_NAME}=nonce".encode())],
            "client": ("test", 123),
            "server": ("localhost", 2024),
        }
    )

    response = await routes.auth_callback(request, code="code", state="state")

    assert response.status_code == 302
    assert captured["mapping"] == {
        "github_login": "jimbo23",
        "work_email": "techshare@nas.io",
        "source": "github_oauth",
        "status": "active",
    }


async def _async_result(value):
    return value


async def _capture_async(captured: dict[str, object], key: str, value):
    captured[key] = value
    return value


@pytest.mark.asyncio
async def test_fetch_github_user_uses_verified_primary_email(monkeypatch) -> None:
    class Response:
        def __init__(self, data, status_code: int = 200) -> None:
            self._data = data
            self.status_code = status_code

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return self._data

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, **kwargs):
            if url.endswith("/user"):
                return Response({"login": "jimbo23", "email": "public-secondary@example.com"})
            return Response(
                [
                    {"email": "unverified@example.com", "primary": True, "verified": False},
                    {"email": "techshare@nas.io", "primary": True, "verified": True},
                ]
            )

    monkeypatch.setattr(oauth.httpx, "AsyncClient", Client)

    user, email = await oauth.fetch_github_user("token")

    assert user["login"] == "jimbo23"
    assert email == "techshare@nas.io"


@pytest.mark.asyncio
async def test_fetch_github_user_rejects_unverified_primary_email(monkeypatch) -> None:
    class Response:
        def __init__(self, data) -> None:
            self._data = data
            self.status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return self._data

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, **kwargs):
            if url.endswith("/user"):
                return Response({"login": "jimbo23", "email": "public@example.com"})
            return Response([{"email": "unverified@example.com", "primary": True}])

    monkeypatch.setattr(oauth.httpx, "AsyncClient", Client)

    _, email = await oauth.fetch_github_user("token")

    assert email is None
