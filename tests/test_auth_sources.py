from __future__ import annotations

import asyncio

import pytest

from agent.utils import auth, github_token


@pytest.fixture(autouse=True)
def _clear_token_source_cache() -> None:
    github_token._GITHUB_TOKEN_CACHE.clear()
    github_token._GITHUB_TOKEN_SOURCES.clear()
    github_token._GITHUB_TOKEN_PRINCIPALS.clear()


def test_leave_failure_comment_posts_generic_token_free_slack_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack auth failures post a generic notice, never the (possibly sensitive) message."""
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://app.example.com")
    thread_called: dict[str, str] = {}

    async def fake_post_slack_thread_reply(channel_id: str, thread_ts: str, message: str) -> bool:
        thread_called["channel_id"] = channel_id
        thread_called["thread_ts"] = thread_ts
        thread_called["message"] = message
        return True

    monkeypatch.setattr(auth, "post_slack_thread_reply", fake_post_slack_thread_reply)
    monkeypatch.setattr(
        auth,
        "get_config",
        lambda: {
            "configurable": {
                "slack_thread": {
                    "channel_id": "C123",
                    "thread_ts": "1.2",
                    "triggering_user_id": "U123",
                }
            }
        },
    )

    # Pass a message that embeds a per-user auth URL; it must NOT be echoed publicly.
    asyncio.run(auth.leave_failure_comment("slack", "Click https://auth.example/secret-token"))

    assert thread_called["channel_id"] == "C123"
    assert thread_called["thread_ts"] == "1.2"
    assert "secret-token" not in thread_called["message"]
    assert "https://app.example.com/my-settings" in thread_called["message"]


def _slack_config(github_login: str | None = "mason-gh") -> dict:
    configurable: dict = {
        "source": "slack",
        "user_email": "mason@example.com",
        "thread_id": "t1",
    }
    if github_login is not None:
        configurable["github_login"] = github_login
    return {"configurable": configurable}


def _github_config(github_login: str = "jimbo23") -> dict:
    return {
        "configurable": {
            "source": "github",
            "github_login": github_login,
            "thread_id": "shared-pr-thread",
        }
    }


def _stub_dashboard_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str | None,
    expires_at: str | None = "2099-01-01T00:00:00Z",
    cached: tuple[str | None, str | None] = (None, None),
) -> None:
    from agent.dashboard import profiles

    async def fake_get_from_thread(thread_id: str):
        return cached

    async def fake_get_valid(login: str):
        return token

    async def fake_get_value(namespace, key):
        return {"token_expires_at": expires_at}

    monkeypatch.setattr(auth, "get_github_token_from_thread", fake_get_from_thread)
    monkeypatch.setattr(profiles, "get_valid_access_token", fake_get_valid)
    monkeypatch.setattr(profiles, "_get_value", fake_get_value)


def test_resolve_github_token_slack_uses_dashboard_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dashboard_store(monkeypatch, token="user-tok")
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: False)

    token, expires_at = asyncio.run(auth.resolve_github_token(_slack_config(), "t1"))

    assert token == "user-tok"
    assert expires_at == "2099-01-01T00:00:00Z"
    assert github_token.get_github_token_source_for_thread("t1") == "user"


def test_bot_installation_token_is_marked_refreshable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_app_token():
        return "bot-tok", "2099-01-01T00:00:00Z"

    monkeypatch.setattr(auth, "get_github_app_installation_token_with_expiry", fake_app_token)

    token, _ = asyncio.run(auth._resolve_bot_installation_token("t1"))

    assert token == "bot-tok"
    assert github_token.get_github_token_source_for_thread("t1") == "app"


def test_resolve_github_token_slack_ignores_stale_thread_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Slack thread ids are shared, so a prior user's cached token must NOT be
    # returned. Resolution always goes by github_login via the dashboard store.
    _stub_dashboard_store(
        monkeypatch,
        token="bob-token",
        cached=("alice-token", "2099-01-01T00:00:00Z"),
    )
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: False)

    token, _ = asyncio.run(auth.resolve_github_token(_slack_config(), "t1"))

    assert token == "bob-token"


def test_resolve_github_token_slack_no_token_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dashboard_store(monkeypatch, token=None)
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: False)

    with pytest.raises(auth.GitHubUserAuthRequired):
        asyncio.run(auth.resolve_github_token(_slack_config(), "t1"))


def test_resolve_github_token_per_user_wins_over_bot_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dashboard_store(monkeypatch, token="user-tok")
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: True)

    async def fail_bot(thread_id: str):
        raise AssertionError("bot token must not be used when a user token exists")

    monkeypatch.setattr(auth, "_resolve_bot_installation_token", fail_bot)

    token, _ = asyncio.run(auth.resolve_github_token(_slack_config(), "t1"))
    assert token == "user-tok"


def test_resolve_github_token_slack_no_token_falls_back_to_bot_in_bot_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dashboard_store(monkeypatch, token=None)
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: True)

    async def fake_bot(thread_id: str):
        return ("bot-tok", None)

    monkeypatch.setattr(auth, "_resolve_bot_installation_token", fake_bot)

    token, expires_at = asyncio.run(auth.resolve_github_token(_slack_config(), "t1"))
    assert (token, expires_at) == ("bot-tok", None)


def test_resolve_github_token_github_uses_dashboard_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dashboard_store(monkeypatch, token="jimbo-token")
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: False)

    token, _ = asyncio.run(auth.resolve_github_token(_github_config(), "shared-pr-thread"))

    assert token == "jimbo-token"
    assert github_token.get_github_token_principal_for_thread("shared-pr-thread") == "jimbo23"


def test_resolve_github_token_github_unmapped_falls_back_to_app_across_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dashboard_store(monkeypatch, token=None)
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: False)

    async def fake_bot(thread_id: str):
        return "app-token", "2099-01-01T00:00:00Z"

    monkeypatch.setattr(auth, "_resolve_bot_installation_token", fake_bot)

    token, _ = asyncio.run(
        auth.resolve_github_token(_github_config("external-user"), "shared-pr-thread")
    )

    assert token == "app-token"


def test_resolve_github_token_github_never_reuses_another_users_cached_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_token.cache_github_token_for_thread(
        "shared-pr-thread",
        "alice-token",
        source="user",
        principal="alice",
    )
    _stub_dashboard_store(
        monkeypatch,
        token=None,
        cached=("alice-token", "2099-01-01T00:00:00Z"),
    )
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: False)

    async def fake_bot(thread_id: str):
        return "app-token", "2099-01-01T00:00:00Z"

    monkeypatch.setattr(auth, "_resolve_bot_installation_token", fake_bot)

    token, _ = asyncio.run(
        auth.resolve_github_token(_github_config("external-user"), "shared-pr-thread")
    )

    assert token == "app-token"


@pytest.mark.parametrize(
    ("source", "principal", "login"),
    [
        ("app", None, "external-user"),
        ("user", "jimbo23", "jimbo23"),
    ],
    ids=["app", "same-user"],
)
def test_resolve_github_token_github_reuses_safe_cached_token(
    monkeypatch: pytest.MonkeyPatch,
    source: github_token.GitHubTokenSource,
    principal: str | None,
    login: str,
) -> None:
    github_token.cache_github_token_for_thread(
        "shared-pr-thread",
        "cached-token",
        source=source,
        principal=principal,
    )
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: False)

    async def fail_user(*args):
        raise AssertionError("safe cache hit must not query dashboard OAuth")

    async def fail_app(*args):
        raise AssertionError("safe cache hit must not mint an App token")

    monkeypatch.setattr(auth, "_resolve_dashboard_user_token", fail_user)
    monkeypatch.setattr(auth, "_resolve_bot_installation_token", fail_app)

    token, _ = asyncio.run(auth.resolve_github_token(_github_config(login), "shared-pr-thread"))

    assert token == "cached-token"


@pytest.mark.parametrize("source", ["github", "linear"])
def test_resolve_github_token_bot_only_mode_non_slack_uses_bot(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setattr(auth, "is_bot_token_only_mode", lambda: True)

    async def fake_bot(thread_id: str):
        return ("bot-tok", None)

    monkeypatch.setattr(auth, "_resolve_bot_installation_token", fake_bot)

    config = {"configurable": {"source": source, "github_login": "octo", "thread_id": "t1"}}
    token, _ = asyncio.run(auth.resolve_github_token(config, "t1"))
    assert token == "bot-tok"
