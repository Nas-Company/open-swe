from __future__ import annotations

import base64
import contextlib
import os
import posixpath
import re
import secrets
import shlex
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath

import modal
from deepagents.backends.protocol import ExecuteResponse, FileDownloadResponse, FileUploadResponse
from langchain_modal import ModalSandbox

PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright:v1.61.0-noble"
PYTHON_VERSION = "3.12"
BUN_VERSION = "1.2.21"
PLAYWRIGHT_VERSION = "1.61.0"
SANDBOX_OWNER_TAG = "open-swe-owner"
SANDBOX_OWNER_VALUE = "open-swe"
SANDBOX_APP_TAG = "open-swe-app"
SANDBOX_APP_ID_TAG = "open-swe-app-id"
GITHUB_BROKER_WORKDIR = "/broker"
_GITHUB_REPO_PATH = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_GITHUB_HTTPS_URL = re.compile(
    rf"^https://github\.com/(?P<owner>{_GITHUB_REPO_PATH})/(?P<repo>{_GITHUB_REPO_PATH})(?:\.git)?/?$",
    re.IGNORECASE,
)
_GITHUB_SSH_URL = re.compile(
    rf"^git@github\.com:(?P<owner>{_GITHUB_REPO_PATH})/(?P<repo>{_GITHUB_REPO_PATH})(?:\.git)?$",
    re.IGNORECASE,
)
_FULL_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_GIT_REF = re.compile(r"^(?:refs/(?:heads|tags)/)?[A-Za-z0-9][A-Za-z0-9._/-]*$")
_GH_COMMAND_PATTERN = re.compile(
    r"(?:^|[;&|(\s])(?:command\s+|sudo\s+)?(?:[^\s;&|()]+/)?gh(?=\s|$)"
)
_GIT_NETWORK_COMMAND_PATTERN = re.compile(
    r"(?:^|[;&|(\s])(?:command\s+|sudo\s+)?(?:[^\s;&|()]+/)?git(?:\s+-C\s+\S+)?"
    r"\s+(?:clone|fetch|pull|push|ls-remote|submodule)(?=\s|$)"
)
_SHELL_TOKENS = frozenset({";", "&&", "||", "|", "&", "<", ">", "(", ")"})
_GIT_NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "ls-remote", "push"})
_GH_DENIED_COMMANDS = frozenset({"alias", "auth", "config", "extension"})
_GH_ALLOWED_COMMANDS = frozenset(
    {
        "--help",
        "--version",
        "api",
        "help",
        "issue",
        "label",
        "pr",
        "repo",
        "ruleset",
        "run",
        "search",
        "status",
        "version",
        "workflow",
    }
)
_GH_DENIED_OPTIONS = frozenset(
    {
        "--body-file",
        "--hostname",
        "--input",
        "--jq",
        "--recover",
        "--template",
        "-q",
        "-t",
    }
)
_GH_GLOBAL_OPTIONS_WITH_VALUE = frozenset({"--repo", "-R"})
_SENSITIVE_PROCESS_PATHS = (
    "/proc/",
    "/sys/",
    "/dev/fd/",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
)


def _command_uses_github(command: str) -> bool:
    return bool(_GH_COMMAND_PATTERN.search(command)) or bool(
        _GIT_NETWORK_COMMAND_PATTERN.search(command)
    )


@dataclass(frozen=True)
class _AuthenticatedCommand:
    executable: str
    argv: tuple[str, ...]
    workdir: str | None


class _UnsafeAuthenticatedCommand(ValueError):
    pass


def _tokenize_command(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError as exc:
        raise _UnsafeAuthenticatedCommand("Authenticated command has invalid quoting") from exc


def _strip_token_placeholders(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    if remaining and remaining[0] == "env":
        remaining.pop(0)
    while remaining and (
        remaining[0].startswith("GH_TOKEN=") or remaining[0].startswith("GITHUB_TOKEN=")
    ):
        remaining.pop(0)
    if remaining and remaining[0] == "command":
        remaining.pop(0)
    return remaining


def _gh_subcommand(argv: list[str]) -> tuple[str | None, int]:
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in _GH_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(argument.startswith(f"{option}=") for option in _GH_GLOBAL_OPTIONS_WITH_VALUE):
            index += 1
            continue
        if argument in {"--help", "--version"}:
            return argument, index
        if argument.startswith("-"):
            raise _UnsafeAuthenticatedCommand(
                f"Unsupported gh global option before subcommand: {argument}"
            )
        return argument, index
    return None, index


def _gh_api_field_reads_file(argument: str) -> bool:
    _key, separator, value = argument.partition("=")
    return bool(separator and value.startswith("@"))


def _validate_gh_file_inputs(argv: list[str], *, subcommand: str) -> None:
    index = 1
    while index < len(argv):
        argument = argv[index]
        option = argument.split("=", 1)[0]
        if option.startswith("--") and "file" in option.casefold():
            raise _UnsafeAuthenticatedCommand(
                f"gh option {option} is unavailable because authenticated commands cannot read files"
            )

        if subcommand != "api" and (argument == "-F" or argument.startswith("-F")):
            raise _UnsafeAuthenticatedCommand(
                "gh -F is unavailable because it reads a file for non-API commands"
            )

        field_value: str | None = None
        if subcommand == "api":
            if argument in {"-F", "--field"}:
                if index + 1 >= len(argv):
                    raise _UnsafeAuthenticatedCommand(f"gh api {argument} requires a value")
                field_value = argv[index + 1]
                index += 1
            elif argument.startswith("-F") and argument != "-F":
                field_value = argument[2:]
            elif argument.startswith("--field="):
                field_value = argument.split("=", 1)[1]
        if field_value is not None and _gh_api_field_reads_file(field_value):
            raise _UnsafeAuthenticatedCommand(
                "gh api fields cannot read values from files inside authenticated sandboxes"
            )
        index += 1


def _validate_gh_argv(argv: list[str]) -> None:
    subcommand, index = _gh_subcommand(argv)
    if subcommand is None:
        raise _UnsafeAuthenticatedCommand("gh requires an explicit subcommand")
    if subcommand in _GH_DENIED_COMMANDS:
        raise _UnsafeAuthenticatedCommand(
            f"gh {subcommand} is unavailable inside authenticated sandboxes"
        )
    if subcommand not in _GH_ALLOWED_COMMANDS:
        raise _UnsafeAuthenticatedCommand(
            f"gh {subcommand} is unavailable inside authenticated sandboxes"
        )
    action = argv[index + 1] if index + 1 < len(argv) else None
    if subcommand == "repo" and action not in {"list", "view"}:
        raise _UnsafeAuthenticatedCommand(
            "Only gh repo list/view are available inside authenticated sandboxes"
        )
    if subcommand == "pr" and action in {"checkout", "create", "merge"}:
        raise _UnsafeAuthenticatedCommand(
            f"gh {subcommand} {action} is unavailable because child git processes would inherit "
            "the token or bypass the guarded PR workflow"
        )
    _validate_gh_file_inputs(argv, subcommand=subcommand)
    for argument in argv[1:]:
        if argument in _GH_DENIED_OPTIONS or any(
            argument.startswith(f"{option}=") for option in _GH_DENIED_OPTIONS
        ):
            raise _UnsafeAuthenticatedCommand(
                f"gh option {argument.split('=', 1)[0]} is unavailable inside authenticated sandboxes"
            )
        lowered = argument.lower()
        if any(path in lowered for path in _SENSITIVE_PROCESS_PATHS):
            raise _UnsafeAuthenticatedCommand(
                "gh commands cannot read process, device, or kernel pseudo-files"
            )


def _git_subcommand(argv: list[str]) -> tuple[str | None, int]:
    index = 1
    while index < len(argv) and argv[index] == "-C":
        if index + 1 >= len(argv):
            raise _UnsafeAuthenticatedCommand("git -C requires a directory")
        index += 2
    if index >= len(argv):
        return None, index
    if argv[index].startswith("-"):
        return None, index
    return argv[index], index


def _validate_git_argv(argv: list[str], subcommand: str, index: int) -> None:
    if subcommand == "clone" and not ({"--no-checkout", "-n"} & set(argv[index + 1 :])):
        raise _UnsafeAuthenticatedCommand(
            "git clone with sandbox credentials requires --no-checkout; run checkout separately "
            "without credentials"
        )
    denied_options = (
        "--config",
        "--exec",
        "--receive-pack",
        "--recurse-submodules",
        "--recursive",
        "--template",
        "--upload-pack",
    )
    for argument in argv[index + 1 :]:
        if (
            argument in denied_options
            or (argument == "-c" and subcommand == "clone")
            or (argument == "-u" and subcommand != "push")
            or any(argument.startswith(f"{option}=") for option in denied_options)
        ):
            raise _UnsafeAuthenticatedCommand(
                f"git option {argument.split('=', 1)[0]} is unavailable with sandbox credentials"
            )


def _parse_authenticated_command(command: str) -> _AuthenticatedCommand | None:
    tokens = _tokenize_command(command)
    workdir: str | None = None
    if len(tokens) >= 3 and tokens[0] == "cd" and tokens[2] == "&&":
        workdir = tokens[1]
        tokens = tokens[3:]

    if any(token in _SHELL_TOKENS or token in {"$", "`"} for token in tokens):
        if _command_uses_github(command):
            raise _UnsafeAuthenticatedCommand("Authenticated git/gh commands must run alone")
        return None

    tokens = _strip_token_placeholders(tokens)
    if not tokens:
        return None
    executable = PurePosixPath(tokens[0]).name
    if executable == "gh":
        if workdir is not None:
            raise _UnsafeAuthenticatedCommand(
                "Authenticated gh commands cannot use cd/workdir because it may resolve through "
                "a symlink or pseudo-filesystem; pass --repo explicitly"
            )
        _validate_gh_argv(tokens)
        return _AuthenticatedCommand("gh", ("/usr/bin/gh", *tokens[1:]), workdir)
    if executable != "git":
        return None

    subcommand, index = _git_subcommand(tokens)
    if subcommand in {"pull", "submodule"}:
        raise _UnsafeAuthenticatedCommand(
            f"Authenticated git {subcommand} is unavailable because checkout/filter child "
            "processes could inherit credentials; fetch first, then update the worktree in a "
            "separate unauthenticated command"
        )
    if subcommand not in _GIT_NETWORK_SUBCOMMANDS:
        return None
    _validate_git_argv(tokens, subcommand, index)
    return _AuthenticatedCommand("git", ("/usr/bin/git", *tokens[1:]), workdir)


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


@dataclass(frozen=True)
class ModalSandboxConfig:
    app_name: str
    timeout_seconds: int
    idle_timeout_seconds: int
    workdir: str
    cpu: float
    memory_mib: int
    github_broker_timeout_seconds: int = 10 * 60
    github_broker_max_transfer_mib: int = 512

    @classmethod
    def from_env(cls) -> ModalSandboxConfig:
        app_name = os.getenv("MODAL_APP_NAME", "open-swe").strip()
        if not app_name:
            raise ValueError("MODAL_APP_NAME must not be empty")

        timeout_seconds = _positive_int_env("MODAL_SANDBOX_TIMEOUT_SECONDS", 4 * 60 * 60)
        idle_timeout_seconds = _positive_int_env("MODAL_SANDBOX_IDLE_TIMEOUT_SECONDS", 30 * 60)
        if idle_timeout_seconds > timeout_seconds:
            raise ValueError(
                "MODAL_SANDBOX_IDLE_TIMEOUT_SECONDS must not exceed MODAL_SANDBOX_TIMEOUT_SECONDS"
            )

        workdir = os.getenv("MODAL_SANDBOX_WORKDIR", "/workspace").strip()
        path = PurePosixPath(workdir)
        if not workdir or not path.is_absolute() or ".." in path.parts:
            raise ValueError("MODAL_SANDBOX_WORKDIR must be an absolute path without '..'")

        return cls(
            app_name=app_name,
            timeout_seconds=timeout_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            workdir=workdir,
            cpu=_positive_float_env("MODAL_SANDBOX_CPU", 2.0),
            memory_mib=_positive_int_env("MODAL_SANDBOX_MEMORY_MIB", 4096),
            github_broker_timeout_seconds=_positive_int_env(
                "MODAL_GITHUB_BROKER_TIMEOUT_SECONDS", 10 * 60
            ),
            github_broker_max_transfer_mib=_positive_int_env(
                "MODAL_GITHUB_BROKER_MAX_TRANSFER_MIB", 512
            ),
        )


class ModalSandboxOwnershipError(RuntimeError):
    """Raised when a persisted sandbox is not owned by the configured Modal app."""


def _sandbox_ownership_tags(app_name: str, app_id: str) -> dict[str, str]:
    return {
        SANDBOX_OWNER_TAG: SANDBOX_OWNER_VALUE,
        SANDBOX_APP_TAG: app_name,
        SANDBOX_APP_ID_TAG: app_id,
    }


def _modal_app_id(app: modal.App) -> str:
    app_id = app.app_id
    if not isinstance(app_id, str) or not app_id:
        raise ModalSandboxOwnershipError("Configured Modal app has no resolvable app ID")
    return app_id


def _sandbox_belongs_to_app(sandbox_id: str, app_name: str, app_id: str) -> bool:
    expected_tags = _sandbox_ownership_tags(app_name, app_id)
    return any(
        candidate.object_id == sandbox_id
        for candidate in modal.Sandbox.list(app_id=app_id, tags=expected_tags)
    )


def _require_sandbox_ownership(sandbox_id: str, app_name: str, app_id: str) -> None:
    if _sandbox_belongs_to_app(sandbox_id, app_name, app_id):
        return
    raise ModalSandboxOwnershipError(
        "Refusing to reconnect to a Modal sandbox outside the configured app; "
        "the existing thread will receive a fresh sandbox"
    )


def _build_browser_image(workdir: str) -> modal.Image:
    quoted_workdir = shlex.quote(workdir)
    return (
        modal.Image.from_registry(
            PLAYWRIGHT_IMAGE,
            add_python=PYTHON_VERSION,
        )
        .apt_install(
            "build-essential",
            "ca-certificates",
            "curl",
            "gh",
            "git",
            "jq",
            "openssh-client",
            "ripgrep",
            "unzip",
            "zip",
        )
        .run_commands(
            f"npm install --global bun@{BUN_VERSION} playwright@{PLAYWRIGHT_VERSION}",
            'python -c "import sys; assert sys.version_info[:2] == (3, 12)"',
            "node -e \"if (Number(process.versions.node.split('.')[0]) < 20) process.exit(1)\"",
            f'test "$(bun --version)" = "{BUN_VERSION}"',
            f'test "$(playwright --version)" = "Version {PLAYWRIGHT_VERSION}"',
            "git --version && gh --version && rg --version",
            "find /ms-playwright -maxdepth 1 -type d -name 'chromium-*' -print -quit | grep -q .",
            f"mkdir -p {quoted_workdir}",
        )
        .env(
            {
                "CI": "1",
                "NODE_PATH": "/usr/lib/node_modules",
                "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
            }
        )
    )


def _build_github_broker_image() -> modal.Image:
    return (
        modal.Image.debian_slim(python_version=PYTHON_VERSION)
        .apt_install("ca-certificates", "gh", "git")
        .run_commands(f"mkdir -p {GITHUB_BROKER_WORKDIR}")
    )


def _canonical_github_url(value: str) -> str:
    match = _GITHUB_HTTPS_URL.fullmatch(value) or _GITHUB_SSH_URL.fullmatch(value)
    if match is None:
        raise _UnsafeAuthenticatedCommand(
            "Authenticated git commands require one explicit github.com repository URL"
        )
    repo = match.group("repo")
    if repo.casefold().endswith(".git"):
        repo = repo[:-4]
    if not repo:
        raise _UnsafeAuthenticatedCommand("GitHub repository name must not be empty")
    return f"https://github.com/{match.group('owner')}/{repo}.git"


def _git_workdir(command: _AuthenticatedCommand, default_workdir: str) -> str:
    argv = list(command.argv)
    if len(argv) >= 3 and argv[1] == "-C":
        return argv[2]
    return command.workdir or default_workdir


def _resolve_sandbox_path(base: str, value: str) -> str:
    candidate = PurePosixPath(value)
    if not candidate.is_absolute():
        candidate = PurePosixPath(base) / candidate
    normalized = posixpath.normpath(str(candidate))
    if not normalized.startswith("/"):
        raise _UnsafeAuthenticatedCommand("Sandbox repository paths must resolve absolutely")
    return normalized


def _process_response(process: object) -> ExecuteResponse:
    process.wait()
    stdout = process.stdout.read()
    stderr = process.stderr.read()
    output = stdout or ""
    if stderr:
        output += "\n" + stderr if output else stderr
    return ExecuteResponse(
        output=output,
        exit_code=process.returncode,
        truncated=False,
    )


def _binary_file_size(sandbox: modal.Sandbox, path: str, *, timeout: int) -> int:
    response = _process_response(sandbox.exec("/usr/bin/stat", "-c", "%s", path, timeout=timeout))
    if response.exit_code != 0:
        raise RuntimeError(f"Could not inspect broker transfer artifact: {response.output}")
    try:
        return int(response.output.strip())
    except ValueError as exc:
        raise RuntimeError("Broker transfer artifact returned an invalid size") from exc


def _read_binary_file(
    sandbox: modal.Sandbox,
    path: str,
    *,
    timeout: int,
    max_bytes: int,
) -> bytes:
    size = _binary_file_size(sandbox, path, timeout=timeout)
    if size > max_bytes:
        raise RuntimeError(
            f"GitHub broker artifact is {size} bytes; the configured limit is {max_bytes} bytes"
        )
    content = sandbox.filesystem.read_bytes(path)
    if isinstance(content, memoryview):
        return content.tobytes()
    if isinstance(content, str):
        return content.encode()
    return bytes(content)


def _write_binary_file(sandbox: modal.Sandbox, path: str, content: bytes) -> None:
    sandbox.filesystem.write_bytes(content, path)


def _broker_clean_env() -> dict[str, str | None]:
    return {
        "ALL_PROXY": None,
        "BROWSER": "/usr/bin/true",
        "CURL_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "GH_BROWSER": "/usr/bin/true",
        "GH_DEBUG": None,
        "GH_ENTERPRISE_TOKEN": None,
        "GH_HOST": "github.com",
        "GH_REPO": None,
        "GH_TOKEN": None,
        "GITHUB_ENTERPRISE_TOKEN": None,
        "GITHUB_TOKEN": None,
        "GIT_ASKPASS": "/usr/bin/true",
        "GIT_CONFIG_COUNT": None,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_PARAMETERS": None,
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CURL_VERBOSE": None,
        "GIT_PROXY_COMMAND": None,
        "GIT_SSL_CAINFO": None,
        "GIT_SSL_NO_VERIFY": None,
        "GIT_SSH": None,
        "GIT_SSH_COMMAND": None,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_TRACE": None,
        "GIT_TRACE2": None,
        "GIT_TRACE2_EVENT": None,
        "GIT_TRACE_CURL": None,
        "GIT_TRACE_CURL_NO_DATA": None,
        "HOME": "/broker/home",
        "HTTP_PROXY": None,
        "HTTPS_PROXY": None,
        "LD_LIBRARY_PATH": None,
        "LD_PRELOAD": None,
        "NO_PROXY": None,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "SSL_CERT_DIR": "/etc/ssl/certs",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
        "XDG_CONFIG_HOME": "/broker/config",
        "XDG_DATA_HOME": "/broker/data",
    }


def _broker_auth_env(github_token: str, *, for_gh: bool) -> dict[str, str | None]:
    env = _broker_clean_env()
    if for_gh:
        env.update(
            {
                "GH_CONFIG_DIR": "/broker/gh-config",
                "GH_EDITOR": "/usr/bin/true",
                "GH_PAGER": "/bin/cat",
                "GH_PROMPT_DISABLED": "1",
                "GH_TOKEN": github_token,
                "GITHUB_TOKEN": github_token,
                "GIT_EDITOR": "/usr/bin/true",
                "PAGER": "/bin/cat",
            }
        )
        return env

    credentials = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
    env.update(
        {
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_CONFIG_COUNT": "8",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credentials}",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_CONFIG_KEY_2": "core.hooksPath",
            "GIT_CONFIG_VALUE_2": "/dev/null",
            "GIT_CONFIG_KEY_3": "protocol.ext.allow",
            "GIT_CONFIG_VALUE_3": "never",
            "GIT_CONFIG_KEY_4": "protocol.file.allow",
            "GIT_CONFIG_VALUE_4": "never",
            "GIT_CONFIG_KEY_5": "http.proxy",
            "GIT_CONFIG_VALUE_5": "",
            "GIT_CONFIG_KEY_6": "http.sslVerify",
            "GIT_CONFIG_VALUE_6": "true",
            "GIT_CONFIG_KEY_7": "http.sslCAInfo",
            "GIT_CONFIG_VALUE_7": "/etc/ssl/certs/ca-certificates.crt",
        }
    )
    return env


def _create_github_broker(config: ModalSandboxConfig) -> modal.Sandbox:
    app = modal.App.lookup(config.app_name)
    return modal.Sandbox.create(
        app=app,
        image=_build_github_broker_image(),
        timeout=config.github_broker_timeout_seconds,
        idle_timeout=min(60, config.github_broker_timeout_seconds),
        workdir=GITHUB_BROKER_WORKDIR,
        cpu=1.0,
        memory=1024,
    )


class AuthenticatedModalSandbox(ModalSandbox):
    """Modal backend with GitHub operations isolated in one-shot broker sandboxes."""

    def __init__(
        self,
        *,
        sandbox: modal.Sandbox,
        github_token: str | None = None,
        config: ModalSandboxConfig | None = None,
    ) -> None:
        super().__init__(sandbox=sandbox)
        self._config = config or ModalSandboxConfig.from_env()
        self._github_token = github_token
        self._github_token_lock = threading.Lock()
        self._github_token_overrides: dict[str, deque[str]] = {}

    def _read_file(self, path: str) -> FileDownloadResponse:
        if not path.startswith("/"):
            return FileDownloadResponse(path=path, content=None, error="invalid_path")
        try:
            content = self._sandbox.filesystem.read_bytes(path)
        except modal.exception.SandboxFilesystemNotFoundError:
            return FileDownloadResponse(path=path, content=None, error="file_not_found")
        except modal.exception.SandboxFilesystemIsADirectoryError:
            return FileDownloadResponse(path=path, content=None, error="is_directory")
        except modal.exception.SandboxFilesystemPermissionError:
            return FileDownloadResponse(path=path, content=None, error="permission_denied")
        return FileDownloadResponse(path=path, content=bytes(content), error=None)

    def _write_file(self, path: str, content: bytes) -> FileUploadResponse:
        if not path.startswith("/"):
            return FileUploadResponse(path=path, error="invalid_path")
        try:
            self._sandbox.filesystem.write_bytes(content, path)
        except modal.exception.SandboxFilesystemNotFoundError:
            return FileUploadResponse(path=path, error="file_not_found")
        except modal.exception.SandboxFilesystemIsADirectoryError:
            return FileUploadResponse(path=path, error="is_directory")
        except modal.exception.SandboxFilesystemPermissionError:
            return FileUploadResponse(path=path, error="permission_denied")
        return FileUploadResponse(path=path, error=None)

    def set_github_token(self, github_token: str | None) -> None:
        with self._github_token_lock:
            self._github_token = github_token

    def set_github_token_for_command(self, github_token: str, command: str) -> None:
        if not github_token or not command:
            raise ValueError("github_token and command are required")
        with self._github_token_lock:
            self._github_token_overrides.setdefault(command, deque()).append(github_token)

    def clear_github_token_for_command(self, github_token: str, command: str) -> bool:
        """Revoke one queued command override if its guarded execution never consumed it."""
        with self._github_token_lock:
            overrides = self._github_token_overrides.get(command)
            if not overrides:
                return False
            try:
                overrides.remove(github_token)
            except ValueError:
                return False
            if not overrides:
                self._github_token_overrides.pop(command, None)
            return True

    @property
    def _broker_timeout(self) -> int:
        return self._config.github_broker_timeout_seconds

    @property
    def _max_transfer_bytes(self) -> int:
        return self._config.github_broker_max_transfer_mib * 1024 * 1024

    def _target_exec(
        self,
        *argv: str,
        timeout: int,
        workdir: str | None = None,
    ) -> ExecuteResponse:
        env = {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null",
            "GIT_CONFIG_KEY_1": "protocol.ext.allow",
            "GIT_CONFIG_VALUE_1": "never",
            "GIT_CONFIG_KEY_2": "protocol.file.allow",
            "GIT_CONFIG_VALUE_2": "always",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        kwargs: dict[str, object] = {"timeout": timeout, "env": env}
        if workdir is not None:
            kwargs["workdir"] = workdir
        return _process_response(self._sandbox.exec(*argv, **kwargs))

    def _broker_exec(
        self,
        broker: modal.Sandbox,
        *argv: str,
        timeout: int,
        github_token: str | None = None,
        for_gh: bool = False,
    ) -> ExecuteResponse:
        env = (
            _broker_auth_env(github_token, for_gh=for_gh)
            if github_token is not None
            else _broker_clean_env()
        )
        return _process_response(broker.exec(*argv, timeout=timeout, env=env))

    def _broker_init_bare(self, broker: modal.Sandbox, *, timeout: int) -> ExecuteResponse:
        return self._broker_exec(
            broker,
            "/usr/bin/git",
            "init",
            "--bare",
            "/broker/repo.git",
            timeout=timeout,
        )

    def _broker_bundle(self, broker: modal.Sandbox, *, timeout: int) -> bytes:
        bundled = self._broker_exec(
            broker,
            "/usr/bin/git",
            "--git-dir=/broker/repo.git",
            "bundle",
            "create",
            "/broker/output.bundle",
            "--all",
            timeout=timeout,
        )
        if bundled.exit_code != 0:
            raise RuntimeError(
                f"GitHub broker could not create a repository bundle: {bundled.output}"
            )
        return _read_binary_file(
            broker,
            "/broker/output.bundle",
            timeout=timeout,
            max_bytes=self._max_transfer_bytes,
        )

    def _target_transfer_path(self) -> str:
        return f"/tmp/open-swe-github-{secrets.token_hex(16)}.bundle"

    def _copy_bundle_to_target(self, content: bytes) -> str:
        path = self._target_transfer_path()
        try:
            _write_binary_file(self._sandbox, path, content)
        except Exception:
            self._remove_target_transfer(path)
            raise
        return path

    def _remove_target_transfer(self, path: str) -> None:
        with contextlib.suppress(Exception):
            self._sandbox.rm(path)

    def _git_parts(self, command: _AuthenticatedCommand) -> tuple[str, list[str]]:
        argv = list(command.argv)
        subcommand, index = _git_subcommand(argv)
        if subcommand is None:
            raise _UnsafeAuthenticatedCommand("Authenticated git command has no subcommand")
        return subcommand, argv[index + 1 :]

    def _parse_clone(self, command: _AuthenticatedCommand) -> tuple[str, str, bool]:
        _subcommand, args = self._git_parts(command)
        quiet = False
        positionals: list[str] = []
        for argument in args:
            if argument in {"--no-checkout", "-n"}:
                continue
            if argument in {"--quiet", "-q"}:
                quiet = True
                continue
            if argument.startswith("-"):
                raise _UnsafeAuthenticatedCommand(
                    f"git clone option {argument} is not supported by the isolated broker"
                )
            positionals.append(argument)
        if len(positionals) != 2:
            raise _UnsafeAuthenticatedCommand(
                "Isolated git clone requires an explicit GitHub URL and destination"
            )
        url = _canonical_github_url(positionals[0])
        base = _git_workdir(command, self._config.workdir)
        destination = _resolve_sandbox_path(base, positionals[1])
        return url, destination, quiet

    def _parse_fetch(
        self, command: _AuthenticatedCommand
    ) -> tuple[str, str, str | None, bool, bool, bool]:
        _subcommand, args = self._git_parts(command)
        quiet = False
        include_tags = True
        prune = False
        positionals: list[str] = []
        for argument in args:
            if argument in {"--quiet", "-q"}:
                quiet = True
            elif argument == "--tags":
                include_tags = True
            elif argument == "--no-tags":
                include_tags = False
            elif argument == "--prune":
                prune = True
            elif argument.startswith("-"):
                raise _UnsafeAuthenticatedCommand(
                    f"git fetch option {argument} is not supported by the isolated broker"
                )
            else:
                positionals.append(argument)
        if not positionals or len(positionals) > 2:
            raise _UnsafeAuthenticatedCommand(
                "Isolated git fetch requires origin and at most one ref"
            )
        remote = "origin" if positionals[0] == "origin" else _canonical_github_url(positionals[0])
        requested_ref = positionals[1] if len(positionals) == 2 else None
        if requested_ref is not None and not (
            _FULL_COMMIT_SHA.fullmatch(requested_ref) or _SAFE_GIT_REF.fullmatch(requested_ref)
        ):
            raise _UnsafeAuthenticatedCommand("git fetch ref is not safe for isolated transfer")
        return (
            _git_workdir(command, self._config.workdir),
            remote,
            requested_ref,
            quiet,
            include_tags,
            prune,
        )

    def _target_origin_url(self, repo_dir: str, *, timeout: int) -> str:
        response = self._target_exec(
            "/usr/bin/git",
            "-C",
            repo_dir,
            "remote",
            "get-url",
            "origin",
            timeout=timeout,
        )
        if response.exit_code != 0:
            raise _UnsafeAuthenticatedCommand(
                f"Could not resolve origin for isolated GitHub access: {response.output}"
            )
        lines = [line.strip() for line in response.output.splitlines() if line.strip()]
        if len(lines) != 1:
            raise _UnsafeAuthenticatedCommand("origin must resolve to exactly one GitHub URL")
        return _canonical_github_url(lines[0])

    def _execute_clone(
        self,
        broker: modal.Sandbox,
        command: _AuthenticatedCommand,
        github_token: str,
        *,
        timeout: int,
    ) -> ExecuteResponse:
        url, destination, quiet = self._parse_clone(command)
        initialized = self._broker_init_bare(broker, timeout=timeout)
        if initialized.exit_code != 0:
            return initialized
        head = self._broker_exec(
            broker,
            "/usr/bin/git",
            "ls-remote",
            "--symref",
            url,
            "HEAD",
            timeout=timeout,
            github_token=github_token,
        )
        if head.exit_code != 0:
            return head
        fetched = self._broker_exec(
            broker,
            "/usr/bin/git",
            "--git-dir=/broker/repo.git",
            "fetch",
            "--no-write-fetch-head",
            url,
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
            timeout=timeout,
            github_token=github_token,
        )
        if fetched.exit_code != 0:
            return fetched
        first_line = next(
            (line for line in head.output.splitlines() if line.startswith("ref: ")), ""
        )
        default_ref = first_line.split("\t", 1)[0].removeprefix("ref: ")
        if default_ref.startswith("refs/heads/") and _SAFE_GIT_REF.fullmatch(default_ref):
            symbolic = self._broker_exec(
                broker,
                "/usr/bin/git",
                "--git-dir=/broker/repo.git",
                "symbolic-ref",
                "HEAD",
                default_ref,
                timeout=timeout,
            )
            if symbolic.exit_code != 0:
                return symbolic
        bundle = self._broker_bundle(broker, timeout=timeout)
        transfer = self._copy_bundle_to_target(bundle)
        try:
            argv = ["/usr/bin/git", "clone", "--no-checkout"]
            if quiet:
                argv.append("--quiet")
            argv.extend([transfer, destination])
            cloned = self._target_exec(*argv, timeout=timeout)
            if cloned.exit_code != 0:
                return cloned
            configured = self._target_exec(
                "/usr/bin/git",
                "-C",
                destination,
                "remote",
                "set-url",
                "origin",
                url,
                timeout=timeout,
            )
            return configured if configured.exit_code != 0 else cloned
        finally:
            self._remove_target_transfer(transfer)

    def _execute_fetch(
        self,
        broker: modal.Sandbox,
        command: _AuthenticatedCommand,
        github_token: str,
        *,
        timeout: int,
    ) -> ExecuteResponse:
        repo_dir, remote, requested_ref, quiet, include_tags, prune = self._parse_fetch(command)
        url = self._target_origin_url(repo_dir, timeout=timeout) if remote == "origin" else remote
        initialized = self._broker_init_bare(broker, timeout=timeout)
        if initialized.exit_code != 0:
            return initialized
        fetch_argv = [
            "/usr/bin/git",
            "--git-dir=/broker/repo.git",
            "fetch",
            "--no-write-fetch-head",
            url,
        ]
        if requested_ref is None:
            fetch_argv.append("+refs/heads/*:refs/heads/*")
            if include_tags:
                fetch_argv.append("+refs/tags/*:refs/tags/*")
        else:
            fetch_argv.append(f"{requested_ref}:refs/heads/open-swe-transfer")
        fetched = self._broker_exec(
            broker,
            *fetch_argv,
            timeout=timeout,
            github_token=github_token,
        )
        if fetched.exit_code != 0:
            return fetched
        bundle = self._broker_bundle(broker, timeout=timeout)
        transfer = self._copy_bundle_to_target(bundle)
        try:
            import_argv = ["/usr/bin/git", "-C", repo_dir, "fetch"]
            if quiet:
                import_argv.append("--quiet")
            if prune:
                import_argv.append("--prune")
            import_argv.append(transfer)
            if requested_ref is None:
                import_argv.append("+refs/heads/*:refs/remotes/origin/*")
                if include_tags:
                    import_argv.append("+refs/tags/*:refs/tags/*")
            else:
                import_argv.append("refs/heads/open-swe-transfer")
            return self._target_exec(*import_argv, timeout=timeout)
        finally:
            self._remove_target_transfer(transfer)

    def _execute_ls_remote(
        self,
        broker: modal.Sandbox,
        command: _AuthenticatedCommand,
        github_token: str,
        *,
        timeout: int,
    ) -> ExecuteResponse:
        _subcommand, args = self._git_parts(command)
        allowed_options = {"--exit-code", "--heads", "--refs", "--symref", "--tags"}
        rebuilt: list[str] = []
        remote_seen = False
        repo_dir = _git_workdir(command, self._config.workdir)
        for argument in args:
            if not remote_seen and argument.startswith("-"):
                if argument not in allowed_options:
                    raise _UnsafeAuthenticatedCommand(
                        f"git ls-remote option {argument} is not supported by the isolated broker"
                    )
                rebuilt.append(argument)
                continue
            if not remote_seen:
                url = (
                    self._target_origin_url(repo_dir, timeout=timeout)
                    if argument == "origin"
                    else _canonical_github_url(argument)
                )
                rebuilt.append(url)
                remote_seen = True
                continue
            if not _SAFE_GIT_REF.fullmatch(argument):
                raise _UnsafeAuthenticatedCommand("git ls-remote pattern is not safe")
            rebuilt.append(argument)
        if not remote_seen:
            raise _UnsafeAuthenticatedCommand("git ls-remote requires an explicit remote")
        return self._broker_exec(
            broker,
            "/usr/bin/git",
            "ls-remote",
            *rebuilt,
            timeout=timeout,
            github_token=github_token,
        )

    def _execute_push(
        self,
        broker: modal.Sandbox,
        command: _AuthenticatedCommand,
        github_token: str,
        *,
        timeout: int,
    ) -> ExecuteResponse:
        _subcommand, args = self._git_parts(command)
        if len(args) != 2:
            raise _UnsafeAuthenticatedCommand(
                "Isolated git push requires the verified URL and one fixed refspec"
            )
        url = _canonical_github_url(args[0])
        source, separator, target = args[1].partition(":")
        if (
            not separator
            or not _FULL_COMMIT_SHA.fullmatch(source)
            or not target.startswith("refs/heads/")
            or not _SAFE_GIT_REF.fullmatch(target)
        ):
            raise _UnsafeAuthenticatedCommand(
                "Isolated git push requires a full commit SHA and refs/heads target"
            )
        repo_dir = _git_workdir(command, self._config.workdir)
        resolved = self._target_exec(
            "/usr/bin/git",
            "-C",
            repo_dir,
            "rev-parse",
            f"{source}^{{commit}}",
            timeout=timeout,
        )
        if resolved.exit_code != 0 or resolved.output.strip() != source:
            raise _UnsafeAuthenticatedCommand(
                "The isolated push source is not the verified commit in the sandbox"
            )
        transfer = self._target_transfer_path()
        transfer_ref = f"refs/open-swe-transfer/{secrets.token_hex(16)}"
        try:
            referenced = self._target_exec(
                "/usr/bin/git",
                "-C",
                repo_dir,
                "update-ref",
                transfer_ref,
                source,
                timeout=timeout,
            )
            if referenced.exit_code != 0:
                return referenced
            bundled = self._target_exec(
                "/usr/bin/git",
                "-C",
                repo_dir,
                "bundle",
                "create",
                transfer,
                transfer_ref,
                timeout=timeout,
            )
            if bundled.exit_code != 0:
                return bundled
            content = _read_binary_file(
                self._sandbox,
                transfer,
                timeout=timeout,
                max_bytes=self._max_transfer_bytes,
            )
        finally:
            self._target_exec(
                "/usr/bin/git",
                "-C",
                repo_dir,
                "update-ref",
                "-d",
                transfer_ref,
                timeout=timeout,
            )
            self._remove_target_transfer(transfer)
        _write_binary_file(broker, "/broker/input.bundle", content)
        initialized = self._broker_init_bare(broker, timeout=timeout)
        if initialized.exit_code != 0:
            return initialized
        imported = self._broker_exec(
            broker,
            "/usr/bin/git",
            "--git-dir=/broker/repo.git",
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "/broker/input.bundle",
            f"{transfer_ref}:refs/heads/open-swe-source",
            timeout=timeout,
        )
        if imported.exit_code != 0:
            return imported
        imported_sha = self._broker_exec(
            broker,
            "/usr/bin/git",
            "--git-dir=/broker/repo.git",
            "rev-parse",
            "refs/heads/open-swe-source",
            timeout=timeout,
        )
        if imported_sha.exit_code != 0 or imported_sha.output.strip() != source:
            raise RuntimeError("GitHub broker bundle did not resolve to the verified push SHA")
        return self._broker_exec(
            broker,
            "/usr/bin/git",
            "--git-dir=/broker/repo.git",
            "push",
            url,
            f"refs/heads/open-swe-source:{target}",
            timeout=timeout,
            github_token=github_token,
        )

    def _execute_authenticated(
        self,
        command: _AuthenticatedCommand,
        github_token: str,
        *,
        timeout: int,
    ) -> ExecuteResponse:
        broker = _create_github_broker(self._config)
        broker_timeout = min(timeout, self._broker_timeout)
        try:
            if command.executable == "gh":
                return self._broker_exec(
                    broker,
                    *command.argv,
                    timeout=broker_timeout,
                    github_token=github_token,
                    for_gh=True,
                )
            subcommand, _args = self._git_parts(command)
            if subcommand == "clone":
                return self._execute_clone(broker, command, github_token, timeout=broker_timeout)
            if subcommand == "fetch":
                return self._execute_fetch(broker, command, github_token, timeout=broker_timeout)
            if subcommand == "ls-remote":
                return self._execute_ls_remote(
                    broker, command, github_token, timeout=broker_timeout
                )
            if subcommand == "push":
                return self._execute_push(broker, command, github_token, timeout=broker_timeout)
            raise _UnsafeAuthenticatedCommand(
                f"git {subcommand} is not supported by the isolated broker"
            )
        finally:
            with contextlib.suppress(Exception):
                broker.terminate()
            with contextlib.suppress(Exception):
                broker.detach()

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        effective_timeout = timeout if timeout is not None else self._default_timeout
        try:
            authenticated_command = _parse_authenticated_command(command)
        except _UnsafeAuthenticatedCommand as exc:
            return ExecuteResponse(
                output=(
                    f"{exc}. Run repository-controlled install, build, test, and inspection "
                    "commands in a separate execute call."
                ),
                exit_code=2,
                truncated=False,
            )

        if authenticated_command is None and _command_uses_github(command):
            return ExecuteResponse(
                output=(
                    "Authenticated git/gh command could not be safely isolated. Run a standalone "
                    "git network command or gh command without shell operators."
                ),
                exit_code=2,
                truncated=False,
            )

        with self._github_token_lock:
            overrides = self._github_token_overrides.get(command)
            github_token = overrides.popleft() if overrides else None
            if overrides is not None and not overrides:
                self._github_token_overrides.pop(command, None)
            github_token = github_token or self._github_token

        if authenticated_command is not None and not github_token:
            return ExecuteResponse(
                output="GitHub authentication is unavailable for this sandbox command.",
                exit_code=2,
                truncated=False,
            )

        if authenticated_command is not None and github_token is not None:
            try:
                response = self._execute_authenticated(
                    authenticated_command,
                    github_token,
                    timeout=effective_timeout,
                )
                credentials = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
                redacted = response.output.replace(github_token, "[REDACTED]").replace(
                    credentials, "[REDACTED]"
                )
                return ExecuteResponse(
                    output=redacted,
                    exit_code=response.exit_code,
                    truncated=response.truncated,
                )
            except _UnsafeAuthenticatedCommand as exc:
                return ExecuteResponse(output=str(exc), exit_code=2, truncated=False)
            except Exception as exc:
                error = str(exc).replace(github_token, "[REDACTED]")
                credentials = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
                error = error.replace(credentials, "[REDACTED]")
                return ExecuteResponse(
                    output=f"Isolated GitHub broker failed: {error}",
                    exit_code=1,
                    truncated=False,
                )

        return _process_response(
            self._sandbox.exec("bash", "-c", command, timeout=effective_timeout, env=None)
        )


def create_modal_sandbox(
    sandbox_id: str | None = None,
    *,
    github_token: str | None = None,
) -> AuthenticatedModalSandbox:
    """Create or reconnect to a browser-ready Modal sandbox."""
    config = ModalSandboxConfig.from_env()
    app = modal.App.lookup(config.app_name)
    app_id = _modal_app_id(app)

    if sandbox_id:
        _require_sandbox_ownership(sandbox_id, config.app_name, app_id)
        sandbox = modal.Sandbox.from_id(sandbox_id)
    else:
        sandbox = modal.Sandbox.create(
            app=app,
            image=_build_browser_image(config.workdir),
            timeout=config.timeout_seconds,
            idle_timeout=config.idle_timeout_seconds,
            workdir=config.workdir,
            cpu=config.cpu,
            memory=config.memory_mib,
        )
        try:
            sandbox.set_tags(_sandbox_ownership_tags(config.app_name, app_id))
            _require_sandbox_ownership(sandbox.object_id, config.app_name, app_id)
        except Exception:
            with contextlib.suppress(Exception):
                sandbox.terminate()
            with contextlib.suppress(Exception):
                sandbox.detach()
            raise

    return AuthenticatedModalSandbox(
        sandbox=sandbox,
        github_token=github_token,
        config=config,
    )
