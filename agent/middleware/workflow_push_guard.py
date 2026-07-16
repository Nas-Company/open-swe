"""Gate workflow-file pushes on human approval."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.config import get_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from ..dashboard.workflow_approval import (
    ensure_workflow_push_pending,
    mark_workflow_push_notified,
    workflow_push_approved,
)
from ..tools.slack_thread_reply import build_workflow_approval_blocks
from ..utils.github_app import (
    RUNTIME_PROXY_TOKEN_PERMISSIONS,
    WORKFLOW_RUNTIME_PROXY_TOKEN_PERMISSIONS,
    get_github_app_installation_token_with_expiry,
)
from ..utils.github_proxy import (
    proxy_token_is_app_managed,
    proxy_token_repositories,
    refresh_proxy_token,
)
from ..utils.sandbox_state import SANDBOX_BACKENDS
from ..utils.slack import post_slack_thread_reply_with_ts

logger = logging.getLogger(__name__)

_WORKFLOW_PREFIX = ".github/workflows/"
_SHELL_OPERATORS = {";", "|", "||", "&"}
_REF_NAME = re.compile(r"^[A-Za-z0-9._/@+-]+$")
_GIT_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40,64}$")
_UNSAFE_RAW_COMMAND = re.compile(r"[;|`$<>\n\r]")
_GIT_PUSH_ATTEMPT = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[^\s;&|()]+/)?git(?:\s+(?![;&|()])[^\s]+)*?\s+push"
    r"(?=\s|[;&|()]|$)",
    re.IGNORECASE,
)
_GITHUB_REPO_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class ParsedGitPush:
    repo_dir: str | None
    remote: str
    local_ref: str
    remote_ref: str
    set_upstream: bool = False


@dataclass(frozen=True)
class WorkflowPushChange:
    fingerprint: str
    repo: str
    branch: str
    base_sha: str
    head_sha: str
    files: list[str]
    remote: str
    local_ref: str
    remote_ref: str
    fixed_command: str
    target_owner: str
    target_repo: str


@dataclass(frozen=True)
class GitPushInspection:
    parsed: ParsedGitPush
    repo_dir: str
    repo: str
    branch: str
    head_sha: str
    fixed_command: str
    fingerprint: str
    workflow_change: WorkflowPushChange | None


@dataclass(frozen=True)
class GitPushRejection:
    reason: str


@dataclass(frozen=True)
class GitInspectResult:
    output: str
    ok: bool
    exit_code: int | None


def _tool_name(request: ToolCallRequest) -> str | None:
    tool_call = getattr(request, "tool_call", None)
    if isinstance(tool_call, Mapping):
        name = tool_call.get("name")
        return name if isinstance(name, str) else None
    return None


def _tool_args(request: ToolCallRequest) -> dict[str, Any]:
    tool_call = getattr(request, "tool_call", None)
    args = tool_call.get("args") if isinstance(tool_call, Mapping) else None
    return dict(args) if isinstance(args, Mapping) else {}


def _tool_call_id(request: ToolCallRequest) -> str | None:
    tool_call = getattr(request, "tool_call", None)
    if isinstance(tool_call, Mapping):
        value = tool_call.get("id")
        return value if isinstance(value, str) else None
    return None


def _config(request: ToolCallRequest) -> Mapping[str, Any]:
    runtime_config = getattr(getattr(request, "runtime", None), "config", None)
    if isinstance(runtime_config, Mapping):
        return runtime_config
    try:
        config = get_config()
    except Exception:
        return {}
    return config if isinstance(config, Mapping) else {}


def _configurable(request: ToolCallRequest) -> Mapping[str, Any]:
    config = _config(request)
    configurable = config.get("configurable")
    return configurable if isinstance(configurable, Mapping) else {}


def _thread_id(request: ToolCallRequest) -> str | None:
    thread_id = _configurable(request).get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _backend(thread_id: str | None) -> Any | None:
    return SANDBOX_BACKENDS.get(thread_id) if thread_id else None


def _response_output(response: Any) -> str:
    output = getattr(response, "output", None)
    if isinstance(output, str):
        return "" if output.strip() == "<no output>" else output
    if isinstance(response, Mapping):
        value = response.get("output")
        if isinstance(value, str):
            return "" if value.strip() == "<no output>" else value
    return str(response or "")


def _response_ok(response: Any) -> bool:
    exit_code = _response_exit_code(response)
    return exit_code == 0


def _response_exit_code(response: Any) -> int | None:
    exit_code = getattr(response, "exit_code", None)
    if isinstance(exit_code, int):
        return exit_code
    if isinstance(response, Mapping):
        value = response.get("exit_code")
        if isinstance(value, int):
            return value
    return None


def _parse_git_push(command: str) -> ParsedGitPush | None:
    stripped = command.strip()
    if _UNSAFE_RAW_COMMAND.search(stripped) or "&" in stripped.replace("&&", ""):
        return None
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return None
    if not tokens:
        return None

    if len(tokens) >= 4 and tokens[0] == "cd" and tokens[2] == "&&":
        if any(token in _SHELL_OPERATORS or token == "&&" for token in tokens[3:]):
            return None
        return _parse_git_tokens(tokens[3:], repo_dir=tokens[1])

    if any(token in _SHELL_OPERATORS or token == "&&" for token in tokens):
        return None
    return _parse_git_tokens(tokens, repo_dir=None)


def _looks_like_git_push(command: str) -> bool:
    probe = command.translate(str.maketrans("", "", "'\"\\"))
    return bool(_GIT_PUSH_ATTEMPT.search(probe))


def _parse_git_tokens(tokens: list[str], *, repo_dir: str | None) -> ParsedGitPush | None:
    if not tokens or tokens[0] != "git":
        return None
    i = 1
    while i < len(tokens) and tokens[i] != "push":
        if tokens[i] == "-C" and i + 1 < len(tokens):
            repo_dir = tokens[i + 1]
            i += 2
            continue
        return None
    if i >= len(tokens) or tokens[i] != "push":
        return None
    return _parse_push_args(tokens[i + 1 :], repo_dir=repo_dir)


def _parse_push_args(tokens: list[str], *, repo_dir: str | None) -> ParsedGitPush | None:
    set_upstream = False
    while tokens and tokens[0] in {"-u", "--set-upstream"}:
        set_upstream = True
        tokens = tokens[1:]
    if len(tokens) != 2 or tokens[0] != "origin":
        return None
    parsed = _parse_refspec(tokens[1])
    if parsed is None:
        return None
    local_ref, remote_ref = parsed
    return ParsedGitPush(
        repo_dir=repo_dir,
        remote="origin",
        local_ref=local_ref,
        remote_ref=remote_ref,
        set_upstream=set_upstream,
    )


def _parse_refspec(refspec: str) -> tuple[str, str] | None:
    if refspec.startswith("-") or ".." in refspec:
        return None
    if ":" in refspec:
        parts = refspec.split(":")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        local_ref, remote_ref = parts
    else:
        local_ref = remote_ref = refspec
    if not _safe_ref(local_ref, allow_head=True) or not _safe_ref(remote_ref, allow_head=False):
        return None
    return local_ref, remote_ref


def _safe_ref(ref: str, *, allow_head: bool) -> bool:
    if allow_head and ref == "HEAD":
        return True
    if ref == "HEAD" or not _REF_NAME.fullmatch(ref):
        return False
    return not any(part in {"", ".", ".."} for part in ref.split("/"))


def _git_command(repo_dir: str | None, args: str) -> str:
    if repo_dir:
        return f"git -C {shlex.quote(repo_dir)} {args}"
    return f"git {args}"


def _run_git(backend: Any, repo_dir: str | None, args: str) -> GitInspectResult:
    try:
        response = backend.execute(_git_command(repo_dir, args), timeout=30)
    except Exception:
        logger.debug("workflow push inspection failed for git %s", args, exc_info=True)
        return GitInspectResult("", False, None)
    return GitInspectResult(
        _response_output(response).strip(),
        _response_ok(response),
        _response_exit_code(response),
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _github_target(remote: str) -> tuple[str, str, str] | None:
    value = remote.strip()
    ssh_match = re.fullmatch(
        r"git@github\.com:([A-Za-z0-9][A-Za-z0-9_.-]*)/"
        r"([A-Za-z0-9][A-Za-z0-9_.-]*?)(?:\.git)?",
        value,
        flags=re.IGNORECASE,
    )
    if ssh_match:
        owner, repo = ssh_match.groups()
    else:
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme not in {"https", "ssh"} or (parsed.hostname or "").lower() != "github.com":
            return None
        if parsed.query or parsed.fragment or port:
            return None
        if parsed.scheme == "https" and (parsed.username or parsed.password):
            return None
        if parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            return None
        owner, repo = parts
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
    if not _GITHUB_REPO_COMPONENT.fullmatch(owner) or not _GITHUB_REPO_COMPONENT.fullmatch(repo):
        return None
    owner = owner.lower()
    repo = repo.lower()
    return owner, repo, f"https://github.com/{owner}/{repo}"


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _remote_ref_sha(
    backend: Any,
    repo_dir: str,
    push_url: str,
    ref: str,
) -> tuple[str | None, GitPushRejection | None]:
    result = _run_git(
        backend,
        repo_dir,
        f"ls-remote --refs {shlex.quote(push_url)} {shlex.quote(ref)}",
    )
    if not result.ok:
        return None, GitPushRejection("the destination branch state could not be read from GitHub")
    lines = [line.split() for line in result.output.splitlines() if line.strip()]
    if not lines:
        return None, None
    if len(lines) != 1 or len(lines[0]) != 2:
        return None, GitPushRejection("the destination branch returned an ambiguous remote state")
    sha, returned_ref = lines[0]
    if returned_ref != ref or not _GIT_OBJECT_ID.fullmatch(sha):
        return None, GitPushRejection("the destination branch returned an invalid remote state")
    return sha, None


def _ensure_remote_commit(
    backend: Any,
    repo_dir: str,
    push_url: str,
    sha: str,
) -> GitPushRejection | None:
    present = _run_git(backend, repo_dir, f"cat-file -e {shlex.quote(sha)}^{{commit}}")
    if present.ok:
        return None
    fetched = _run_git(
        backend,
        repo_dir,
        f"fetch {shlex.quote(push_url)} {shlex.quote(sha)} --quiet",
    )
    if not fetched.ok:
        return GitPushRejection("the verified destination commit could not be fetched")
    present = _run_git(backend, repo_dir, f"cat-file -e {shlex.quote(sha)}^{{commit}}")
    if not present.ok:
        return GitPushRejection("the fetched destination commit could not be verified locally")
    return None


def _remote_default_sha(
    backend: Any,
    repo_dir: str,
    push_url: str,
) -> tuple[str | None, GitPushRejection | None]:
    result = _run_git(
        backend,
        repo_dir,
        f"ls-remote --symref {shlex.quote(push_url)} HEAD",
    )
    if not result.ok:
        return None, GitPushRejection(
            "the destination default branch could not be read from GitHub"
        )
    if not result.output.strip():
        return None, None
    head_shas: list[str] = []
    for line in result.output.splitlines():
        fields = line.split()
        if line.startswith("ref: "):
            continue
        if len(fields) == 2 and fields[1] == "HEAD" and _GIT_OBJECT_ID.fullmatch(fields[0]):
            head_shas.append(fields[0])
    if len(head_shas) != 1:
        return None, GitPushRejection("the destination default branch returned an invalid state")
    return head_shas[0], None


def _run_coroutine_sync(coro: Awaitable[ToolMessage | Command]) -> ToolMessage | Command:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, ToolMessage | Command | BaseException] = {}

    def target() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            result["value"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    value = result["value"]
    if isinstance(value, BaseException):
        raise value
    return value


def _inspect_git_push(backend: Any, parsed: ParsedGitPush) -> GitPushInspection | GitPushRejection:
    root_result = _run_git(backend, parsed.repo_dir, "rev-parse --show-toplevel")
    if not root_result.ok:
        return GitPushRejection("the repository root could not be verified")
    root = _first_line(root_result.output)
    if not root:
        return GitPushRejection("the repository root could not be verified")

    remote = _run_git(backend, root, "remote get-url --push --all origin")
    push_urls = [line.strip() for line in remote.output.splitlines() if line.strip()]
    if not remote.ok or len(push_urls) != 1:
        return GitPushRejection("origin must have exactly one verifiable push URL")
    target = _github_target(push_urls[0])
    if target is None:
        return GitPushRejection("origin's push URL must identify one GitHub repository")
    target_owner, target_repo, repo = target
    push_url = f"{repo}.git"

    branch = _run_git(backend, root, "rev-parse --abbrev-ref HEAD")
    branch_name = _first_line(branch.output) if branch.ok else ""
    if not branch_name or branch_name == "HEAD" or parsed.remote_ref != branch_name:
        return GitPushRejection("the push must target the checked-out branch")
    if parsed.local_ref not in {"HEAD", branch_name}:
        return GitPushRejection("the push source must be HEAD or the checked-out branch")

    target_sha = _run_git(backend, root, f"rev-parse {shlex.quote(parsed.local_ref)}")
    head = _first_line(target_sha.output) if target_sha.ok else ""
    if not head or not _GIT_OBJECT_ID.fullmatch(head):
        return GitPushRejection("the exact commit to push could not be verified")

    destination_ref = f"refs/heads/{parsed.remote_ref}"
    remote_sha, remote_rejection = _remote_ref_sha(
        backend,
        root,
        push_url,
        destination_ref,
    )
    if remote_rejection is not None:
        return remote_rejection
    if remote_sha is not None:
        unavailable = _ensure_remote_commit(backend, root, push_url, remote_sha)
        if unavailable is not None:
            return unavailable
        base_sha = remote_sha
    else:
        default_sha, default_rejection = _remote_default_sha(backend, root, push_url)
        if default_rejection is not None:
            return default_rejection
        if default_sha is None:
            empty_tree = _run_git(backend, root, "hash-object -t tree /dev/null")
            base_sha = _first_line(empty_tree.output) if empty_tree.ok else ""
            if not _GIT_OBJECT_ID.fullmatch(base_sha):
                return GitPushRejection(
                    "the empty repository comparison base could not be verified"
                )
        else:
            unavailable = _ensure_remote_commit(backend, root, push_url, default_sha)
            if unavailable is not None:
                return unavailable
            merge_base = _run_git(
                backend,
                root,
                f"merge-base {shlex.quote(head)} {shlex.quote(default_sha)}",
            )
            base_sha = _first_line(merge_base.output) if merge_base.ok else ""
            if not _GIT_OBJECT_ID.fullmatch(base_sha):
                return GitPushRejection("the comparison base could not be verified")

    range_expr = f"{shlex.quote(base_sha)} {shlex.quote(head)}"

    names = _run_git(
        backend,
        root,
        f"diff --name-only --diff-filter=ACMRTD {range_expr} -- .github/workflows",
    )
    if not names.ok:
        return GitPushRejection("workflow file changes could not be inspected")
    files = sorted(
        line.strip()
        for line in names.output.splitlines()
        if line.strip().startswith(_WORKFLOW_PREFIX)
    )
    diff_output = ""
    if files:
        diff = _run_git(
            backend,
            root,
            f"diff --binary --full-index {range_expr} -- .github/workflows",
        )
        if not diff.ok or not diff.output:
            return GitPushRejection("the workflow diff could not be verified")
        diff_output = diff.output

    fixed_refspec = f"{head}:refs/heads/{parsed.remote_ref}"
    fixed_args = ["push", push_url, fixed_refspec]
    fixed_command = _git_command(root, " ".join(shlex.quote(arg) for arg in fixed_args))
    payload = {
        "repo": repo,
        "target_owner": target_owner,
        "target_repo": target_repo,
        "push_url": push_url,
        "branch": branch_name,
        "base_sha": base_sha,
        "head_sha": head,
        "files": files,
        "diff": diff_output,
        "remote": parsed.remote,
        "local_ref": parsed.local_ref,
        "remote_ref": parsed.remote_ref,
        "fixed_refspec": fixed_refspec,
    }
    fingerprint = _fingerprint(payload)
    workflow_change = (
        WorkflowPushChange(
            fingerprint=fingerprint,
            repo=repo,
            branch=branch_name,
            base_sha=base_sha,
            head_sha=head,
            files=files,
            remote=parsed.remote,
            local_ref=parsed.local_ref,
            remote_ref=parsed.remote_ref,
            fixed_command=fixed_command,
            target_owner=target_owner,
            target_repo=target_repo,
        )
        if files
        else None
    )
    return GitPushInspection(
        parsed=parsed,
        repo_dir=root,
        repo=repo,
        branch=branch_name,
        head_sha=head,
        fixed_command=fixed_command,
        fingerprint=fingerprint,
        workflow_change=workflow_change,
    )


def _workflow_change_for_push(backend: Any, parsed: ParsedGitPush) -> WorkflowPushChange | None:
    inspection = _inspect_git_push(backend, parsed)
    return inspection.workflow_change if isinstance(inspection, GitPushInspection) else None


def _blocked_message(change: WorkflowPushChange, *, already_rejected: bool = False) -> ToolMessage:
    status = "rejected" if already_rejected else "approval_required"
    content = {
        "status": "error",
        "error_type": "WorkflowPushApprovalRequired",
        "error": (
            "This git push includes GitHub workflow file changes and requires human "
            "approval before Open SWE can push it. Retry the same standalone git push "
            "after the thread owner approves the workflow diff."
        ),
        "workflow_approval_status": status,
        "fingerprint": change.fingerprint,
        "files": change.files,
        "repo": change.repo,
        "branch": change.branch,
    }
    return ToolMessage(content=json.dumps(content), tool_call_id="", status="error")


def _rejected_push_message(rejection: GitPushRejection) -> ToolMessage:
    content = {
        "status": "error",
        "error_type": "GitPushVerificationFailed",
        "error": (
            "This git push was blocked because Open SWE could not prove its exact source, "
            f"destination, and workflow impact: {rejection.reason}. Retry with a standalone "
            "`git push origin <current-branch>` command after resolving the verification issue."
        ),
    }
    return ToolMessage(content=json.dumps(content), tool_call_id="", status="error")


def _tool_message_for_request(message: ToolMessage, request: ToolCallRequest) -> ToolMessage:
    message.tool_call_id = _tool_call_id(request)
    return message


def _override_execute_command(request: ToolCallRequest, command: str) -> ToolCallRequest:
    tool_call = getattr(request, "tool_call", None)
    if not isinstance(tool_call, Mapping):
        return request
    args = dict(_tool_args(request))
    args["command"] = command
    return request.override(tool_call={**dict(tool_call), "args": args})


def _approval_slack_message(change: WorkflowPushChange) -> str:
    files = "\n".join(f"• `{path}`" for path in change.files[:10])
    if len(change.files) > 10:
        files += f"\n• …and {len(change.files) - 10} more"
    repo = change.repo or "the repository"
    branch = change.branch or "the current branch"
    return (
        "*Workflow file approval required*\n"
        f"Open SWE is trying to push changes to GitHub workflow files in `{repo}` on `{branch}`.\n\n"
        f"*Files:*\n{files}\n\n"
        f"*Fingerprint:* `{change.fingerprint}`\n\n"
        "Approve only if this exact workflow diff is expected. If the workflow files change, "
        "a new fingerprint will be required."
    )


async def _post_slack_approval_if_needed(
    request: ToolCallRequest, change: WorkflowPushChange, record: Mapping[str, Any]
) -> None:
    if record.get("notified") is True:
        return
    configurable = _configurable(request)
    slack_thread = configurable.get("slack_thread")
    if not isinstance(slack_thread, Mapping):
        return
    channel_id = slack_thread.get("channel_id")
    thread_ts = slack_thread.get("thread_ts")
    if not isinstance(channel_id, str) or not isinstance(thread_ts, str):
        return
    message = _approval_slack_message(change)
    message_ts, error = await post_slack_thread_reply_with_ts(
        channel_id,
        thread_ts,
        message,
        blocks=build_workflow_approval_blocks(message, change.fingerprint),
    )
    if message_ts and not error:
        thread_id = _thread_id(request)
        if thread_id:
            await mark_workflow_push_notified(thread_id, change.fingerprint)


async def _approval_state(request: ToolCallRequest, change: WorkflowPushChange) -> str:
    thread_id = _thread_id(request)
    if not thread_id:
        return "missing_thread"
    try:
        if await workflow_push_approved(thread_id, change.fingerprint):
            return "approved"
        record, _created = await ensure_workflow_push_pending(
            thread_id,
            fingerprint=change.fingerprint,
            repo=change.repo,
            branch=change.branch,
            base_sha=change.base_sha,
            head_sha=change.head_sha,
            files=change.files,
        )
        await _post_slack_approval_if_needed(request, change, record)
        return str(record.get("status") or "pending")
    except Exception:
        logger.exception("Failed to read or write workflow push approval state")
        return "approval_error"


async def _run_with_workflow_token(
    thread_id: str,
    repo: str,
    command: str,
    run: Callable[[], Awaitable[ToolMessage | Command]],
) -> ToolMessage | Command:
    repo_name = repo.rsplit("/", 1)[-1] if repo else ""
    repositories = [repo_name] if repo_name else None
    if not proxy_token_is_app_managed(thread_id):
        return await run()
    recorded_repositories = proxy_token_repositories(thread_id)
    if recorded_repositories is not None and repo_name.casefold() not in {
        recorded.casefold() for recorded in recorded_repositories
    }:
        raise RuntimeError(
            "Workflow push was not executed because its repository is outside the sandbox auth scope"
        )
    if os.getenv("SANDBOX_TYPE", "langsmith") == "modal":
        token, _expires_at = await get_github_app_installation_token_with_expiry(
            repositories=repositories,
            permissions=WORKFLOW_RUNTIME_PROXY_TOKEN_PERMISSIONS,
        )
        if not token:
            raise RuntimeError("Workflow push was not executed because elevated GitHub auth failed")
        backend = SANDBOX_BACKENDS.get(thread_id)
        setter = getattr(backend, "set_github_token_for_command", None)
        clearer = getattr(backend, "clear_github_token_for_command", None)
        if not callable(setter) or not callable(clearer):
            raise RuntimeError("Modal sandbox does not support command-scoped workflow auth")
        setter(token, command)
        try:
            return await run()
        finally:
            clearer(token, command)

    elevated = await refresh_proxy_token(
        thread_id,
        repositories=repositories,
        permissions=WORKFLOW_RUNTIME_PROXY_TOKEN_PERMISSIONS,
    )
    if not elevated:
        raise RuntimeError("Workflow push was not executed because elevated GitHub auth failed")
    try:
        return await run()
    finally:
        await refresh_proxy_token(
            thread_id,
            repositories=repositories,
            permissions=RUNTIME_PROXY_TOKEN_PERMISSIONS,
        )


class WorkflowPushGuardMiddleware(AgentMiddleware):
    """Require approval before pushing `.github/workflows` changes."""

    state_schema = AgentState

    def _push_for_request(
        self, request: ToolCallRequest
    ) -> GitPushInspection | GitPushRejection | None:
        if _tool_name(request) != "execute":
            return None
        command = _tool_args(request).get("command")
        if not isinstance(command, str):
            return None
        parsed = _parse_git_push(command)
        if parsed is None:
            if _looks_like_git_push(command):
                return GitPushRejection(
                    "only a standalone push to origin with one explicit refspec is supported"
                )
            return None
        backend = _backend(_thread_id(request))
        if backend is None:
            return GitPushRejection("the sandbox used to inspect the repository is unavailable")
        return _inspect_git_push(backend, parsed)

    async def _handle_inspection_async(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
        inspection: GitPushInspection,
    ) -> ToolMessage | Command:
        change = inspection.workflow_change
        if change is None:
            return await handler(_override_execute_command(request, inspection.fixed_command))

        thread_id = _thread_id(request)
        state = await _approval_state(request, change)
        if state == "approved" and thread_id:
            backend = _backend(thread_id)
            final_inspection = (
                _inspect_git_push(backend, inspection.parsed) if backend is not None else None
            )
            if (
                not isinstance(final_inspection, GitPushInspection)
                or final_inspection.workflow_change is None
                or final_inspection.fingerprint != inspection.fingerprint
                or final_inspection.fixed_command != inspection.fixed_command
            ):
                return _tool_message_for_request(
                    _rejected_push_message(
                        GitPushRejection(
                            "the repository, commit, workflow diff, or push URL changed after approval"
                        )
                    ),
                    request,
                )
            safe_request = _override_execute_command(request, final_inspection.fixed_command)
            return await _run_with_workflow_token(
                thread_id,
                final_inspection.repo,
                final_inspection.fixed_command,
                lambda: handler(safe_request),
            )
        return _tool_message_for_request(
            _blocked_message(change, already_rejected=state == "rejected"), request
        )

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        push = self._push_for_request(request)
        if push is None:
            return handler(request)
        if isinstance(push, GitPushRejection):
            return _tool_message_for_request(_rejected_push_message(push), request)

        async def run_handler(request_to_run: ToolCallRequest) -> ToolMessage | Command:
            return handler(request_to_run)

        return _run_coroutine_sync(self._handle_inspection_async(request, run_handler, push))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        push = self._push_for_request(request)
        if push is None:
            return await handler(request)
        if isinstance(push, GitPushRejection):
            return _tool_message_for_request(_rejected_push_message(push), request)
        return await self._handle_inspection_async(request, handler, push)
