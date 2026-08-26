from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent.utils import github_app


@pytest.fixture(autouse=True)
def _clear_token_cache() -> Any:
    github_app.clear_app_token_cache()
    yield
    github_app.clear_app_token_cache()


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, str]:
        return {"token": "token", "expires_at": "expires"}


class _FakeAsyncClient:
    last_post: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).last_post = {"url": url, **kwargs}
        return _FakeResponse()


def _configure(monkeypatch: pytest.MonkeyPatch, client_cls: type) -> None:
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "1")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "key")
    monkeypatch.setattr(github_app, "GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setattr(github_app, "_generate_app_jwt", lambda: "jwt")
    monkeypatch.setattr(github_app.httpx, "AsyncClient", client_cls)


class _CountingResponse:
    def __init__(self, expires_at: str) -> None:
        self._expires_at = expires_at

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, str]:
        return {"token": "tok-123", "expires_at": self._expires_at}


class _CountingClient:
    posts = 0
    expires_at = "2099-01-01T00:00:00Z"

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _CountingClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _CountingResponse:
        type(self).posts += 1
        return _CountingResponse(type(self).expires_at)


@pytest.mark.asyncio
async def test_token_is_cached_until_near_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    class Client(_CountingClient):
        posts = 0
        expires_at = future

    _configure(monkeypatch, Client)

    t1, _ = await github_app.get_github_app_installation_token_with_expiry()
    t2, _ = await github_app.get_github_app_installation_token_with_expiry()

    assert t1 == t2 == "tok-123"
    assert Client.posts == 1  # second call served from the in-process cache


@pytest.mark.asyncio
async def test_app_token_401_clears_both_caches_and_mints_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import webapp
    from agent.utils import github_token

    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    class Response:
        def __init__(self, token: str) -> None:
            self.token = token

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, str]:
            return {"token": self.token, "expires_at": future}

    class Client(_CountingClient):
        posts = 0

        async def post(self, url: str, **kwargs: Any) -> Response:
            type(self).posts += 1
            return Response(f"app-token-{type(self).posts}")

    _configure(monkeypatch, Client)
    first, expires_at = await github_app.get_github_app_installation_token_with_expiry()
    github_token.cache_github_token_for_thread(
        "thread-1", first or "", expires_at=expires_at, source="app"
    )

    refreshed = await webapp._refresh_thread_github_token_after_401("thread-1", "")

    assert first == "app-token-1"
    assert refreshed == "app-token-2"
    assert Client.posts == 2
    await github_token.invalidate_cached_github_token("thread-1")


@pytest.mark.asyncio
async def test_cache_is_scoped_per_repository_set(monkeypatch: pytest.MonkeyPatch) -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    class Client(_CountingClient):
        posts = 0
        expires_at = future

    _configure(monkeypatch, Client)

    await github_app.get_github_app_installation_token_with_expiry(repositories=["a"])
    await github_app.get_github_app_installation_token_with_expiry(repositories=["b"])
    await github_app.get_github_app_installation_token_with_expiry(repositories=["a"])

    assert Client.posts == 2  # distinct scopes mint separately; the repeat is cached


@pytest.mark.asyncio
async def test_near_expiry_token_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    soon = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()

    class Client(_CountingClient):
        posts = 0
        expires_at = soon

    _configure(monkeypatch, Client)

    await github_app.get_github_app_installation_token_with_expiry()
    await github_app.get_github_app_installation_token_with_expiry()

    assert Client.posts == 2  # within the safety margin -> re-minted every call


@pytest.mark.asyncio
async def test_installation_token_can_be_scoped_to_repository_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "1")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "key")
    monkeypatch.setattr(github_app, "GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setattr(github_app, "_generate_app_jwt", lambda: "jwt")
    monkeypatch.setattr(github_app.httpx, "AsyncClient", _FakeAsyncClient)

    token, expires_at = await github_app.get_github_app_installation_token_with_expiry(
        repository_ids=[123]
    )

    assert token == "token"
    assert expires_at == "expires"
    assert _FakeAsyncClient.last_post is not None
    assert _FakeAsyncClient.last_post["json"] == {"repository_ids": [123]}


@pytest.mark.asyncio
async def test_installation_token_includes_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "1")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "key")
    monkeypatch.setattr(github_app, "GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setattr(github_app, "_generate_app_jwt", lambda: "jwt")
    monkeypatch.setattr(github_app.httpx, "AsyncClient", _FakeAsyncClient)

    await github_app.get_github_app_installation_token_with_expiry(
        repositories=["open-swe"], permissions={"workflows": "write", "contents": "write"}
    )

    assert _FakeAsyncClient.last_post is not None
    assert _FakeAsyncClient.last_post["json"] == {
        "repositories": ["open-swe"],
        "permissions": {"contents": "write", "workflows": "write"},
    }


@pytest.mark.asyncio
async def test_cache_is_scoped_per_permission_set(monkeypatch: pytest.MonkeyPatch) -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    class Client(_CountingClient):
        posts = 0
        expires_at = future

    _configure(monkeypatch, Client)

    await github_app.get_github_app_installation_token_with_expiry(
        permissions={"contents": "write"}
    )
    await github_app.get_github_app_installation_token_with_expiry(
        permissions={"contents": "write", "workflows": "write"}
    )
    await github_app.get_github_app_installation_token_with_expiry(
        permissions={"contents": "write"}
    )

    assert Client.posts == 2


@pytest.mark.asyncio
async def test_installation_token_omits_scope_for_full_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "1")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "key")
    monkeypatch.setattr(github_app, "GITHUB_APP_INSTALLATION_ID", "2")
    monkeypatch.setattr(github_app, "_generate_app_jwt", lambda: "jwt")
    monkeypatch.setattr(github_app.httpx, "AsyncClient", _FakeAsyncClient)

    await github_app.get_github_app_installation_token_with_expiry()

    assert _FakeAsyncClient.last_post is not None
    assert _FakeAsyncClient.last_post["json"] is None
