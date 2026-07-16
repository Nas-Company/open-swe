from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage

from agent.middleware import workflow_push_guard as guard

_FIXED_PUSH = (
    "git -C /repo push https://github.com/langchain-ai/open-swe.git "
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:refs/heads/feature"
)


class _Response:
    def __init__(self, output: str, exit_code: int = 0) -> None:
        self.output = output
        self.exit_code = exit_code
        self.truncated = False


class _Backend:
    id = "sandbox-id"

    def __init__(
        self,
        *,
        workflow_files: str = ".github/workflows/ci.yml",
        push_urls: list[str] | None = None,
        fail_contains: str | None = None,
    ) -> None:
        self.workflow_files = workflow_files
        self.commands: list[str] = []
        self.github_tokens: list[str | None] = []
        self.github_token_overrides: list[tuple[str, str]] = []
        self.cleared_github_token_overrides: list[tuple[str, str]] = []
        self.head = "a" * 40
        self.base = "b" * 40
        self.push_urls = push_urls or ["git@github.com:langchain-ai/open-swe.git"]
        self.push_url_reads = 0
        self.fail_contains = fail_contains

    def set_github_token(self, token: str | None) -> None:
        self.github_tokens.append(token)

    def set_github_token_for_command(self, token: str, command: str) -> None:
        self.github_token_overrides.append((token, command))

    def clear_github_token_for_command(self, token: str, command: str) -> bool:
        self.cleared_github_token_overrides.append((token, command))
        return True

    def execute(self, command: str, *, timeout: int | None = None) -> _Response:
        self.commands.append(command)
        if self.fail_contains and self.fail_contains in command:
            return _Response("inspection failed", 2)
        if "rev-parse --show-toplevel" in command:
            return _Response("/repo\n")
        if "remote get-url --push --all origin" in command:
            index = min(self.push_url_reads, len(self.push_urls) - 1)
            self.push_url_reads += 1
            return _Response(f"{self.push_urls[index]}\n")
        if "ls-remote --refs" in command:
            return _Response(f"{self.base}\trefs/heads/feature\n")
        if f"cat-file -e {self.base}^{{commit}}" in command:
            return _Response("")
        if "diff --name-only" in command:
            return _Response(f"{self.workflow_files}\n" if self.workflow_files else "")
        if "diff --binary --full-index" in command:
            return _Response("diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n")
        if "rev-parse --abbrev-ref HEAD" in command:
            return _Response("feature\n")
        if "rev-parse HEAD" in command or "rev-parse feature" in command:
            return _Response(f"{self.head}\n")
        return _Response("")


class _Runtime:
    config = {
        "configurable": {
            "thread_id": "thread-1",
            "slack_thread": {"channel_id": "C123", "thread_ts": "1700000000.000100"},
        }
    }


class _Request:
    runtime = _Runtime()

    def __init__(self, command: str = "git -C /repo push origin feature") -> None:
        self.tool_call = {
            "name": "execute",
            "args": {"command": command},
            "id": "call-1",
        }

    def override(self, **kwargs: Any) -> _Request:
        next_request = _Request()
        next_request.tool_call = kwargs.get("tool_call", self.tool_call)
        return next_request


@pytest.fixture(autouse=True)
def _clear_backend_cache() -> Any:
    guard.SANDBOX_BACKENDS.clear()
    yield
    guard.SANDBOX_BACKENDS.clear()


def test_parse_git_push_supports_git_c_and_cd() -> None:
    assert guard._parse_git_push("git -C /repo push origin feature") == guard.ParsedGitPush(
        repo_dir="/repo", remote="origin", local_ref="feature", remote_ref="feature"
    )
    assert guard._parse_git_push(
        "cd /repo && git push -u origin HEAD:feature"
    ) == guard.ParsedGitPush(
        repo_dir="/repo",
        remote="origin",
        local_ref="HEAD",
        remote_ref="feature",
        set_upstream=True,
    )
    assert guard._parse_git_push("git status && git push") is None
    assert guard._parse_git_push("git push origin feature; git push origin evil:feature") is None


@pytest.mark.parametrize(
    "command",
    [
        "git push --all",
        "git push upstream feature",
        "git status && git push origin feature",
        "command 'git' push origin feature",
        "/usr/bin/'git' push origin feature",
    ],
)
async def test_unsupported_git_push_forms_are_blocked(
    command: str,
) -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend()
    called = False

    async def handler(_request: Any) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(command), handler)

    assert called is False
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert json.loads(str(result.content))["error_type"] == "GitPushVerificationFailed"


async def test_git_push_is_blocked_when_inspection_fails() -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend(
        fail_contains="remote get-url --push --all origin"
    )
    called = False

    async def handler(_request: Any) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert called is False
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert json.loads(str(result.content))["error_type"] == "GitPushVerificationFailed"


def test_workflow_change_for_push_fingerprints_workflow_diff() -> None:
    backend = _Backend()
    change = guard._workflow_change_for_push(
        backend,
        guard.ParsedGitPush(
            repo_dir="/repo", remote="origin", local_ref="feature", remote_ref="feature"
        ),
    )

    assert change is not None
    assert change.repo == "https://github.com/langchain-ai/open-swe"
    assert change.branch == "feature"
    assert change.files == [".github/workflows/ci.yml"]
    assert change.fixed_command == _FIXED_PUSH
    assert change.target_owner == "langchain-ai"
    assert change.target_repo == "open-swe"
    assert len(change.fingerprint) == 64


def test_workflow_fingerprint_and_fixed_command_bind_actual_push_url() -> None:
    original = guard._workflow_change_for_push(
        _Backend(),
        guard.ParsedGitPush(
            repo_dir="/repo", remote="origin", local_ref="feature", remote_ref="feature"
        ),
    )
    redirected = guard._workflow_change_for_push(
        _Backend(push_urls=["https://github.com/Nas-Company/other-repo.git"]),
        guard.ParsedGitPush(
            repo_dir="/repo", remote="origin", local_ref="feature", remote_ref="feature"
        ),
    )

    assert original is not None
    assert redirected is not None
    assert redirected.repo == "https://github.com/nas-company/other-repo"
    assert redirected.target_owner == "nas-company"
    assert redirected.target_repo == "other-repo"
    assert redirected.fixed_command.startswith(
        "git -C /repo push https://github.com/nas-company/other-repo.git "
    )
    assert redirected.fingerprint != original.fingerprint


def test_workflow_inspection_uses_remote_github_sha_not_mutable_tracking_ref() -> None:
    backend = _Backend()

    inspection = guard._inspect_git_push(
        backend,
        guard.ParsedGitPush(
            repo_dir="/repo", remote="origin", local_ref="feature", remote_ref="feature"
        ),
    )

    assert isinstance(inspection, guard.GitPushInspection)
    assert inspection.workflow_change is not None
    assert inspection.workflow_change.base_sha == backend.base
    assert any(
        "ls-remote --refs https://github.com/langchain-ai/open-swe.git refs/heads/feature"
        in command
        for command in backend.commands
    )
    assert not any("refs/remotes/origin/feature" in command for command in backend.commands)


async def test_push_fails_closed_when_remote_branch_lookup_fails() -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend(fail_contains="ls-remote --refs")
    called = False

    async def handler(_request: Any) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert called is False
    assert isinstance(result, ToolMessage)
    payload = json.loads(str(result.content))
    assert payload["error_type"] == "GitPushVerificationFailed"
    assert "destination branch state" in payload["error"]


def test_workflow_change_for_push_ignores_non_workflow_push() -> None:
    backend = _Backend(workflow_files="")

    assert (
        guard._workflow_change_for_push(
            backend,
            guard.ParsedGitPush(
                repo_dir="/repo", remote="origin", local_ref="feature", remote_ref="feature"
            ),
        )
        is None
    )


def test_workflow_change_for_push_rejects_non_current_refspec() -> None:
    backend = _Backend()

    assert (
        guard._workflow_change_for_push(
            backend,
            guard.ParsedGitPush(
                repo_dir="/repo", remote="origin", local_ref="evil", remote_ref="feature"
            ),
        )
        is None
    )


async def test_unapproved_workflow_push_blocks_and_posts_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend()
    posted: dict[str, Any] = {}

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return False

    async def fake_pending(thread_id: str, **kwargs: Any) -> tuple[dict[str, Any], bool]:
        return {"fingerprint": kwargs["fingerprint"], "status": "pending", "notified": False}, True

    async def fake_post(
        channel_id: str, thread_ts: str, message: str, **kwargs: Any
    ) -> tuple[str, None]:
        posted.update(
            channel_id=channel_id, thread_ts=thread_ts, message=message, blocks=kwargs["blocks"]
        )
        return "1700000000.000200", None

    async def fake_notified(thread_id: str, fingerprint: str) -> None:
        posted["notified"] = fingerprint

    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(guard, "ensure_workflow_push_pending", fake_pending)
    monkeypatch.setattr(guard, "post_slack_thread_reply_with_ts", fake_post)
    monkeypatch.setattr(guard, "mark_workflow_push_notified", fake_notified)

    called = False

    async def handler(_request: Any) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert called is False
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    payload = json.loads(str(result.content))
    assert payload["workflow_approval_status"] == "approval_required"
    assert payload["files"] == [".github/workflows/ci.yml"]
    assert posted["channel_id"] == "C123"
    assert posted["blocks"][1]["elements"][0]["value"]


async def test_approved_workflow_push_elevates_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend()
    refreshed: list[tuple[list[str] | None, dict[str, str]]] = []

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return True

    async def fake_refresh(
        thread_id: str | None,
        *,
        repositories: list[str] | None,
        permissions: dict[str, str],
    ) -> bool:
        refreshed.append((repositories, dict(permissions)))
        return True

    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(guard, "refresh_proxy_token", fake_refresh)
    monkeypatch.setattr(guard, "proxy_token_is_app_managed", lambda _tid: True)
    monkeypatch.setattr(guard, "proxy_token_repositories", lambda _tid: ("open-swe",))

    pushed_command = ""

    async def handler(request: Any) -> ToolMessage:
        nonlocal pushed_command
        pushed_command = request.tool_call["args"]["command"]
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert isinstance(result, ToolMessage)
    assert result.content == "pushed"
    assert pushed_command == _FIXED_PUSH
    assert refreshed[0][0] == ["open-swe"]
    assert refreshed[0][1]["workflows"] == "write"
    assert refreshed[1][0] == ["open-swe"]
    assert "workflows" not in refreshed[1][1]


async def test_approved_workflow_push_blocks_when_pushurl_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend(
        push_urls=[
            "git@github.com:langchain-ai/open-swe.git",
            "https://github.com/Nas-Company/other-repo.git",
        ]
    )
    guard.SANDBOX_BACKENDS["thread-1"] = backend

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return True

    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(
        guard,
        "refresh_proxy_token",
        AsyncMock(side_effect=AssertionError("changed target must not receive elevated auth")),
    )
    called = False

    async def handler(_request: Any) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert called is False
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    payload = json.loads(str(result.content))
    assert payload["error_type"] == "GitPushVerificationFailed"
    assert "changed after approval" in payload["error"]


def test_sync_approved_workflow_push_uses_fixed_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend()

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return True

    async def fake_refresh(
        thread_id: str | None,
        *,
        repositories: list[str] | None,
        permissions: dict[str, str],
    ) -> bool:
        return True

    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(guard, "refresh_proxy_token", fake_refresh)
    monkeypatch.setattr(guard, "proxy_token_is_app_managed", lambda _tid: True)
    monkeypatch.setattr(guard, "proxy_token_repositories", lambda _tid: ("open-swe",))

    pushed_command = ""

    def handler(request: Any) -> ToolMessage:
        nonlocal pushed_command
        pushed_command = request.tool_call["args"]["command"]
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = guard.WorkflowPushGuardMiddleware().wrap_tool_call(_Request(), handler)

    assert isinstance(result, ToolMessage)
    assert pushed_command == _FIXED_PUSH


async def test_approved_modal_workflow_push_keeps_user_token_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend()
    guard.SANDBOX_BACKENDS["thread-1"] = backend

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return True

    monkeypatch.setenv("SANDBOX_TYPE", "modal")
    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(guard, "proxy_token_is_app_managed", lambda _tid: False)
    monkeypatch.setattr(
        guard,
        "refresh_proxy_token",
        AsyncMock(side_effect=AssertionError("user OAuth must not be replaced")),
    )
    monkeypatch.setattr(
        guard,
        "get_github_app_installation_token_with_expiry",
        AsyncMock(side_effect=AssertionError("user OAuth must not be replaced")),
    )

    async def handler(request: Any) -> ToolMessage:
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert result.content == "pushed"
    assert backend.github_tokens == []
    assert backend.github_token_overrides == []


async def test_approved_modal_app_push_uses_one_shot_command_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend()
    guard.SANDBOX_BACKENDS["thread-1"] = backend

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return True

    app_token = AsyncMock(return_value=("ghs_workflow", "expires"))
    monkeypatch.setenv("SANDBOX_TYPE", "modal")
    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(guard, "proxy_token_is_app_managed", lambda _tid: True)
    monkeypatch.setattr(guard, "proxy_token_repositories", lambda _tid: ("open-swe",))
    monkeypatch.setattr(guard, "get_github_app_installation_token_with_expiry", app_token)

    async def handler(request: Any) -> ToolMessage:
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert result.content == "pushed"
    app_token.assert_awaited_once_with(
        repositories=["open-swe"],
        permissions=guard.WORKFLOW_RUNTIME_PROXY_TOKEN_PERMISSIONS,
    )
    assert backend.github_token_overrides == [("ghs_workflow", _FIXED_PUSH)]
    assert backend.cleared_github_token_overrides == [("ghs_workflow", _FIXED_PUSH)]


async def test_modal_workflow_token_is_revoked_when_handler_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend()
    guard.SANDBOX_BACKENDS["thread-1"] = backend

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return True

    monkeypatch.setenv("SANDBOX_TYPE", "modal")
    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(guard, "proxy_token_is_app_managed", lambda _tid: True)
    monkeypatch.setattr(guard, "proxy_token_repositories", lambda _tid: ("open-swe",))
    monkeypatch.setattr(
        guard,
        "get_github_app_installation_token_with_expiry",
        AsyncMock(return_value=("ghs_workflow", "expires")),
    )

    async def handler(_request: Any) -> ToolMessage:
        raise RuntimeError("execute tool failed before reaching the backend")

    with pytest.raises(RuntimeError, match="before reaching the backend"):
        await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert backend.github_token_overrides == [("ghs_workflow", _FIXED_PUSH)]
    assert backend.cleared_github_token_overrides == [("ghs_workflow", _FIXED_PUSH)]


async def test_app_managed_workflow_push_cannot_expand_repository_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend()

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return True

    monkeypatch.setenv("SANDBOX_TYPE", "modal")
    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(guard, "proxy_token_is_app_managed", lambda _tid: True)
    monkeypatch.setattr(guard, "proxy_token_repositories", lambda _tid: ("other-repo",))
    mint_token = AsyncMock(side_effect=AssertionError("out-of-scope repo must not mint a token"))
    monkeypatch.setattr(guard, "get_github_app_installation_token_with_expiry", mint_token)
    called = False

    async def handler(_request: Any) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="pushed", tool_call_id="call-1")

    with pytest.raises(RuntimeError, match="outside the sandbox auth scope"):
        await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert called is False
    mint_token.assert_not_awaited()


async def test_approved_workflow_push_stops_when_elevation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend()

    async def fake_approved(thread_id: str, fingerprint: str) -> bool:
        return True

    async def fake_refresh(*args: Any, **kwargs: Any) -> bool:
        return False

    called = False

    async def handler(request: Any) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="pushed", tool_call_id="call-1")

    monkeypatch.setattr(guard, "workflow_push_approved", fake_approved)
    monkeypatch.setattr(guard, "refresh_proxy_token", fake_refresh)
    monkeypatch.setattr(guard, "proxy_token_is_app_managed", lambda _tid: True)
    monkeypatch.setattr(guard, "proxy_token_repositories", lambda _tid: ("open-swe",))

    with pytest.raises(RuntimeError, match="was not executed"):
        await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert called is False


async def test_non_workflow_push_runs_without_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    guard.SANDBOX_BACKENDS["thread-1"] = _Backend(workflow_files="")
    called = False
    pushed_command = ""

    async def fail_approval(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("approval should not be checked")

    monkeypatch.setattr(guard, "workflow_push_approved", fail_approval)

    async def handler(request: Any) -> ToolMessage:
        nonlocal called, pushed_command
        called = True
        pushed_command = request.tool_call["args"]["command"]
        return ToolMessage(content="pushed", tool_call_id="call-1")

    result = await guard.WorkflowPushGuardMiddleware().awrap_tool_call(_Request(), handler)

    assert called is True
    assert pushed_command == _FIXED_PUSH
    assert isinstance(result, ToolMessage)
    assert result.content == "pushed"
