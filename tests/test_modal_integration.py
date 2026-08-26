from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock, patch

import pytest
from modal import exception as modal_exception

from agent.integrations.modal import (
    BUN_VERSION,
    PLAYWRIGHT_IMAGE,
    PLAYWRIGHT_VERSION,
    PYTHON_VERSION,
    SANDBOX_APP_ID_TAG,
    SANDBOX_APP_TAG,
    SANDBOX_OWNER_TAG,
    SANDBOX_OWNER_VALUE,
    AuthenticatedModalSandbox,
    ModalSandboxConfig,
    ModalSandboxOwnershipError,
    _build_browser_image,
    _read_binary_file,
    _write_binary_file,
    create_modal_sandbox,
)
from agent.utils.sandbox import create_sandbox
from agent.utils.sandbox_state import SandboxBackendProxy


def _process(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    process = MagicMock(returncode=returncode)
    process.stdout.read.return_value = stdout
    process.stderr.read.return_value = stderr
    return process


def test_browser_image_contains_pinned_report_runtime() -> None:
    base_image = MagicMock()
    apt_image = MagicMock()
    command_image = MagicMock()
    final_image = MagicMock()
    base_image.apt_install.return_value = apt_image
    apt_image.run_commands.return_value = command_image
    command_image.env.return_value = final_image

    with patch(
        "agent.integrations.modal.modal.Image.from_registry",
        return_value=base_image,
    ) as from_registry:
        image = _build_browser_image("/work dir")

    assert image is final_image
    from_registry.assert_called_once_with(PLAYWRIGHT_IMAGE, add_python=PYTHON_VERSION)
    base_image.apt_install.assert_called_once_with(
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
    commands = apt_image.run_commands.call_args.args
    assert f"bun@{BUN_VERSION}" in commands[0]
    assert f"playwright@{PLAYWRIGHT_VERSION}" in commands[0]
    assert "sys.version_info[:2] == (3, 12)" in commands[1]
    assert "< 20" in commands[2]
    assert any("gh --version" in command and "rg --version" in command for command in commands)
    assert any("chromium-*" in command for command in commands)
    assert commands[-1] == "mkdir -p '/work dir'"
    command_image.env.assert_called_once_with(
        {
            "CI": "1",
            "NODE_PATH": "/usr/lib/node_modules",
            "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
        }
    )


def test_binary_transfer_uses_current_modal_filesystem_api() -> None:
    sandbox = MagicMock()
    sandbox.exec.return_value = _process(stdout="4")
    sandbox.filesystem.read_bytes.return_value = b"data"

    assert _read_binary_file(sandbox, "/tmp/input.bundle", timeout=30, max_bytes=10) == b"data"
    _write_binary_file(sandbox, "/tmp/output.bundle", b"safe")

    sandbox.open.assert_not_called()
    sandbox.filesystem.read_bytes.assert_called_once_with("/tmp/input.bundle")
    sandbox.filesystem.write_bytes.assert_called_once_with(b"safe", "/tmp/output.bundle")


def test_backend_file_transfer_uses_current_modal_filesystem_api() -> None:
    sandbox = MagicMock(object_id="sb-current-filesystem")
    sandbox.filesystem.read_bytes.return_value = b"downloaded"
    backend = AuthenticatedModalSandbox(sandbox=sandbox)

    downloads = backend.download_files(["/workspace/input.txt"])
    uploads = backend.upload_files([("/workspace/output.txt", b"uploaded")])

    assert downloads[0].path == "/workspace/input.txt"
    assert downloads[0].content == b"downloaded"
    assert downloads[0].error is None
    assert uploads[0].path == "/workspace/output.txt"
    assert uploads[0].error is None
    sandbox.filesystem.write_bytes.assert_called_once_with(b"uploaded", "/workspace/output.txt")


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (modal_exception.SandboxFilesystemNotFoundError(), "file_not_found"),
        (modal_exception.SandboxFilesystemIsADirectoryError(), "is_directory"),
        (modal_exception.SandboxFilesystemPermissionError(), "permission_denied"),
    ],
)
def test_backend_download_maps_modal_filesystem_errors(
    exception: Exception, expected_error: str
) -> None:
    sandbox = MagicMock(object_id="sb-current-filesystem")
    sandbox.filesystem.read_bytes.side_effect = exception
    backend = AuthenticatedModalSandbox(sandbox=sandbox)

    result = backend.download_files(["/workspace/input.txt"])[0]

    assert result.path == "/workspace/input.txt"
    assert result.content is None
    assert result.error == expected_error


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (modal_exception.SandboxFilesystemNotFoundError(), "file_not_found"),
        (modal_exception.SandboxFilesystemIsADirectoryError(), "is_directory"),
        (modal_exception.SandboxFilesystemPermissionError(), "permission_denied"),
    ],
)
def test_backend_upload_maps_modal_filesystem_errors(
    exception: Exception, expected_error: str
) -> None:
    sandbox = MagicMock(object_id="sb-current-filesystem")
    sandbox.filesystem.write_bytes.side_effect = exception
    backend = AuthenticatedModalSandbox(sandbox=sandbox)

    result = backend.upload_files([("/workspace/output.txt", b"uploaded")])[0]

    assert result.path == "/workspace/output.txt"
    assert result.error == expected_error


def test_modal_config_reads_lifecycle_and_resource_settings() -> None:
    with patch.dict(
        os.environ,
        {
            "MODAL_APP_NAME": "report-agent",
            "MODAL_SANDBOX_TIMEOUT_SECONDS": "7200",
            "MODAL_SANDBOX_IDLE_TIMEOUT_SECONDS": "900",
            "MODAL_SANDBOX_WORKDIR": "/reports",
            "MODAL_SANDBOX_CPU": "3.5",
            "MODAL_SANDBOX_MEMORY_MIB": "6144",
        },
        clear=True,
    ):
        config = ModalSandboxConfig.from_env()

    assert config == ModalSandboxConfig(
        app_name="report-agent",
        timeout_seconds=7200,
        idle_timeout_seconds=900,
        workdir="/reports",
        cpu=3.5,
        memory_mib=6144,
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MODAL_SANDBOX_TIMEOUT_SECONDS", "0", "must be a positive integer"),
        ("MODAL_SANDBOX_CPU", "fast", "must be a positive number"),
        ("MODAL_SANDBOX_WORKDIR", "relative", "must be an absolute path"),
    ],
)
def test_modal_config_rejects_invalid_values(name: str, value: str, message: str) -> None:
    with (
        patch.dict(os.environ, {name: value}, clear=True),
        pytest.raises(ValueError, match=message),
    ):
        ModalSandboxConfig.from_env()


def test_create_modal_sandbox_uses_browser_image_and_configured_lifecycle() -> None:
    app = MagicMock(app_id="ap-report-agent")
    sandbox = MagicMock(object_id="sb-new")
    image = MagicMock()
    token = "ghs_short_lived"
    env = {
        "MODAL_APP_NAME": "report-agent",
        "MODAL_SANDBOX_TIMEOUT_SECONDS": "7200",
        "MODAL_SANDBOX_IDLE_TIMEOUT_SECONDS": "900",
        "MODAL_SANDBOX_WORKDIR": "/reports",
        "MODAL_SANDBOX_CPU": "3.5",
        "MODAL_SANDBOX_MEMORY_MIB": "6144",
    }

    with (
        patch.dict(os.environ, env, clear=True),
        patch("agent.integrations.modal.modal.App.lookup", return_value=app) as lookup,
        patch("agent.integrations.modal.modal.Sandbox.create", return_value=sandbox) as create,
        patch(
            "agent.integrations.modal.modal.Sandbox.list",
            return_value=iter([sandbox]),
        ) as list_sandboxes,
        patch("agent.integrations.modal._build_browser_image", return_value=image) as build_image,
    ):
        backend = create_modal_sandbox(github_token=token)

    assert isinstance(backend, AuthenticatedModalSandbox)
    assert backend.id == "sb-new"
    lookup.assert_called_once_with("report-agent")
    build_image.assert_called_once_with("/reports")
    create.assert_called_once_with(
        app=app,
        image=image,
        timeout=7200,
        idle_timeout=900,
        workdir="/reports",
        cpu=3.5,
        memory=6144,
    )
    sandbox.set_tags.assert_called_once_with(
        {
            SANDBOX_OWNER_TAG: SANDBOX_OWNER_VALUE,
            SANDBOX_APP_TAG: "report-agent",
            SANDBOX_APP_ID_TAG: "ap-report-agent",
        }
    )
    list_sandboxes.assert_called_once_with(
        app_id="ap-report-agent",
        tags={
            SANDBOX_OWNER_TAG: SANDBOX_OWNER_VALUE,
            SANDBOX_APP_TAG: "report-agent",
            SANDBOX_APP_ID_TAG: "ap-report-agent",
        },
    )
    assert token not in repr(create.call_args)


def test_reconnect_uses_modal_1_4_from_id_signature_without_rebuilding_image() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    app = MagicMock(app_id="ap-open-swe")
    with (
        patch("agent.integrations.modal.modal.App.lookup", return_value=app) as lookup,
        patch(
            "agent.integrations.modal.modal.Sandbox.list",
            return_value=iter([MagicMock(object_id="sb-existing")]),
        ) as list_sandboxes,
        patch("agent.integrations.modal.modal.Sandbox.from_id", return_value=sandbox) as from_id,
        patch("agent.integrations.modal.modal.Sandbox.create") as create,
        patch("agent.integrations.modal._build_browser_image") as build_image,
    ):
        backend = create_modal_sandbox("sb-existing", github_token="ghs_token")

    assert backend.id == "sb-existing"
    lookup.assert_called_once_with("open-swe")
    list_sandboxes.assert_called_once_with(
        app_id="ap-open-swe",
        tags={
            SANDBOX_OWNER_TAG: SANDBOX_OWNER_VALUE,
            SANDBOX_APP_TAG: "open-swe",
            SANDBOX_APP_ID_TAG: "ap-open-swe",
        },
    )
    from_id.assert_called_once_with("sb-existing")
    create.assert_not_called()
    build_image.assert_not_called()


@pytest.mark.parametrize("listed_id", [None, "sb-different-open-swe-sandbox"])
def test_reconnect_fails_closed_without_expected_app_membership(
    listed_id: str | None,
) -> None:
    app = MagicMock(app_id="ap-open-swe")
    listed = [] if listed_id is None else [MagicMock(object_id=listed_id)]

    with (
        patch.dict(os.environ, {"MODAL_APP_NAME": "open-swe"}, clear=True),
        patch("agent.integrations.modal.modal.App.lookup", return_value=app) as lookup,
        patch("agent.integrations.modal.modal.Sandbox.list", return_value=iter(listed)) as listing,
        patch("agent.integrations.modal.modal.Sandbox.from_id") as from_id,
        patch("agent.integrations.modal.modal.Sandbox.create") as create,
        pytest.raises(ModalSandboxOwnershipError, match="outside the configured app"),
    ):
        create_modal_sandbox("sb-other-app")

    lookup.assert_called_once_with("open-swe")
    listing.assert_called_once_with(
        app_id="ap-open-swe",
        tags={
            SANDBOX_OWNER_TAG: SANDBOX_OWNER_VALUE,
            SANDBOX_APP_TAG: "open-swe",
            SANDBOX_APP_ID_TAG: "ap-open-swe",
        },
    )
    from_id.assert_not_called()
    create.assert_not_called()


def test_matching_tags_on_another_app_do_not_authorize_reconnect() -> None:
    app = MagicMock(app_id="ap-open-swe")
    foreign = MagicMock(object_id="sb-company-brain")
    foreign.get_tags.return_value = {
        SANDBOX_OWNER_TAG: SANDBOX_OWNER_VALUE,
        SANDBOX_APP_TAG: "open-swe",
        SANDBOX_APP_ID_TAG: "ap-open-swe",
    }

    with (
        patch("agent.integrations.modal.modal.App.lookup", return_value=app),
        patch("agent.integrations.modal.modal.Sandbox.list", return_value=iter([])) as listing,
        patch("agent.integrations.modal.modal.Sandbox.from_id") as from_id,
        pytest.raises(ModalSandboxOwnershipError),
    ):
        create_modal_sandbox(foreign.object_id)

    listing.assert_called_once_with(
        app_id="ap-open-swe",
        tags={
            SANDBOX_OWNER_TAG: SANDBOX_OWNER_VALUE,
            SANDBOX_APP_TAG: "open-swe",
            SANDBOX_APP_ID_TAG: "ap-open-swe",
        },
    )
    from_id.assert_not_called()
    foreign.get_tags.assert_not_called()


def test_new_sandbox_is_terminated_if_ownership_tagging_fails() -> None:
    sandbox = MagicMock(object_id="sb-new")
    sandbox.set_tags.side_effect = RuntimeError("tag service unavailable")
    app = MagicMock(app_id="ap-open-swe")

    with (
        patch("agent.integrations.modal.modal.App.lookup", return_value=app),
        patch("agent.integrations.modal.modal.Sandbox.create", return_value=sandbox),
        patch("agent.integrations.modal._build_browser_image", return_value=MagicMock()),
        pytest.raises(RuntimeError, match="tag service unavailable"),
    ):
        create_modal_sandbox()

    sandbox.terminate.assert_called_once_with()
    sandbox.detach.assert_called_once_with()


def test_new_sandbox_is_terminated_if_app_membership_recheck_fails() -> None:
    sandbox = MagicMock(object_id="sb-new")
    app = MagicMock(app_id="ap-open-swe")

    with (
        patch("agent.integrations.modal.modal.App.lookup", return_value=app),
        patch("agent.integrations.modal.modal.Sandbox.create", return_value=sandbox),
        patch("agent.integrations.modal.modal.Sandbox.list", return_value=iter([])),
        patch("agent.integrations.modal._build_browser_image", return_value=MagicMock()),
        pytest.raises(ModalSandboxOwnershipError),
    ):
        create_modal_sandbox()

    sandbox.set_tags.assert_called_once_with(
        {
            SANDBOX_OWNER_TAG: SANDBOX_OWNER_VALUE,
            SANDBOX_APP_TAG: "open-swe",
            SANDBOX_APP_ID_TAG: "ap-open-swe",
        }
    )
    sandbox.terminate.assert_called_once_with()
    sandbox.detach.assert_called_once_with()


def test_gh_runs_only_in_one_shot_broker_and_never_in_main_sandbox() -> None:
    token = "ghs_short_lived"
    sandbox = MagicMock(object_id="sb-existing")
    broker = MagicMock(object_id="sb-broker")
    broker.exec.return_value = _process(stdout="ok", stderr="warning", returncode=0)
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token=token)

    with patch("agent.integrations.modal._create_github_broker", return_value=broker):
        result = backend.execute("gh repo view", timeout=15)

    sandbox.exec.assert_not_called()
    sandbox.open.assert_not_called()
    broker.exec.assert_called_once()
    assert broker.exec.call_args.args == ("/usr/bin/gh", "repo", "view")
    assert broker.exec.call_args.kwargs["timeout"] == 15
    exec_env = broker.exec.call_args.kwargs["env"]
    assert exec_env["GH_TOKEN"] == token
    assert exec_env["GITHUB_TOKEN"] == token
    assert exec_env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert exec_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert exec_env["HTTPS_PROXY"] is None
    assert exec_env["GH_HOST"] == "github.com"
    assert exec_env["GH_DEBUG"] is None
    assert exec_env["GIT_TRACE_CURL"] is None
    assert exec_env["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert token not in broker.exec.call_args.args
    broker.terminate.assert_called_once_with()
    broker.detach.assert_called_once_with()
    assert result.output == "ok\nwarning"
    assert result.exit_code == 0


def test_broker_is_terminated_when_authenticated_execution_raises() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    broker = MagicMock(object_id="sb-broker")
    broker.exec.side_effect = RuntimeError("broker failed")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="ghs_app")

    with patch("agent.integrations.modal._create_github_broker", return_value=broker):
        result = backend.execute("gh repo view")

    assert result.exit_code == 1
    assert "broker failed" in result.output
    broker.terminate.assert_called_once_with()
    broker.detach.assert_called_once_with()
    sandbox.exec.assert_not_called()


def test_execute_does_not_inject_github_token_into_repo_controlled_build() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    sandbox.exec.return_value = _process(stdout="built")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="gho_user")

    backend.execute("bun install --frozen-lockfile && bun run build")

    assert sandbox.exec.call_args.kwargs["env"] is None


@pytest.mark.parametrize(
    "command",
    [
        "git clone https://github.com/Nas-Company/nas-reporting.git && bun install",
        "git fetch origin; env",
        "gh repo view | jq .name",
        "git fetch origin && $(env)",
    ],
)
def test_execute_rejects_compound_authenticated_commands(command: str) -> None:
    sandbox = MagicMock(object_id="sb-existing")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="gho_user")

    result = backend.execute(command)

    sandbox.exec.assert_not_called()
    assert result.exit_code == 2
    assert "must run alone" in result.output


@pytest.mark.parametrize(
    "command",
    [
        "gh auth token",
        "gh alias set leak '!env'",
        "gh extension exec sample",
        "gh repo clone Nas-Company/open-swe",
        "gh pr checkout 123",
        "gh pr create --title test --body test",
        "gh release upload v1 /tmp/environ-link --repo Nas-Company/open-swe",
        "gh made-up-alias",
        "gh api repos/Nas-Company/open-swe --input /proc/self/environ",
        "gh api repos/Nas-Company/open-swe/issues -F body=@/tmp/environ-link",
        "gh api repos/Nas-Company/open-swe/issues --field=body=@relative-link",
        "cd /proc/self && gh api repos/Nas-Company/open-swe/issues -F body=@environ",
        "cd /tmp/workspace-link && gh repo view --repo Nas-Company/open-swe",
        "gh pr view --json number --jq env.GH_TOKEN",
        "git clone --template=/workspace/hooks https://github.com/org/repo.git",
        "git fetch --upload-pack=/workspace/leak origin",
        "git pull origin main",
        "git submodule foreach env",
        "git submodule update --init",
        "git clone https://github.com/org/repo.git",
        "git clone --no-checkout --recurse-submodules https://github.com/org/repo.git",
    ],
)
def test_execute_rejects_authenticated_commands_that_can_expose_process_auth(
    command: str,
) -> None:
    sandbox = MagicMock(object_id="sb-existing")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="ghs_app")

    result = backend.execute(command)

    sandbox.exec.assert_not_called()
    assert result.exit_code == 2


def test_authenticated_command_fails_closed_without_app_token() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    backend = AuthenticatedModalSandbox(sandbox=sandbox)

    result = backend.execute("gh repo view")

    sandbox.exec.assert_not_called()
    assert result.exit_code == 2
    assert "authentication is unavailable" in result.output


def test_clone_transfers_only_bundle_bytes_to_main_sandbox() -> None:
    token = "ghs_repo_scoped"
    bundle = b"safe git bundle"
    sandbox = MagicMock(object_id="sb-existing")
    broker = MagicMock(object_id="sb-broker")
    broker.filesystem.read_bytes.return_value = bundle

    def broker_exec(*args: str, **_kwargs: object) -> MagicMock:
        if args[:3] == ("/usr/bin/git", "ls-remote", "--symref"):
            return _process(stdout="ref: refs/heads/main\tHEAD\nabc\tHEAD\n")
        if args[:3] == ("/usr/bin/stat", "-c", "%s"):
            return _process(stdout=str(len(bundle)))
        return _process()

    broker.exec.side_effect = broker_exec
    sandbox.exec.return_value = _process(stdout="cloned")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token=token)

    with patch("agent.integrations.modal._create_github_broker", return_value=broker):
        result = backend.execute(
            "git clone --no-checkout https://github.com/Nas-Company/nas-reporting.git report"
        )

    assert result.exit_code == 0
    sandbox.filesystem.write_bytes.assert_called_once_with(bundle, sandbox.rm.call_args.args[0])
    assert all(token not in repr(call) for call in sandbox.exec.call_args_list)
    assert token not in repr(sandbox.filesystem.write_bytes.call_args_list)
    assert any(
        call.args[:3] == ("/usr/bin/git", "clone", "--no-checkout")
        and call.args[-1] == "/workspace/report"
        for call in sandbox.exec.call_args_list
    )
    sandbox.rm.assert_called_once()
    broker.terminate.assert_called_once_with()
    broker.detach.assert_called_once_with()


def test_fetch_resolves_fixed_origin_in_main_and_authenticates_only_broker() -> None:
    token = "ghs_repo_scoped"
    bundle = b"fetched objects"
    sandbox = MagicMock(object_id="sb-existing")
    broker = MagicMock(object_id="sb-broker")
    broker.filesystem.read_bytes.return_value = bundle

    target_responses = iter(
        [
            _process(stdout="https://github.com/Nas-Company/nas-reporting.git\n"),
            _process(stdout="updated"),
        ]
    )
    sandbox.exec.side_effect = lambda *_args, **_kwargs: next(target_responses)

    def broker_exec(*args: str, **_kwargs: object) -> MagicMock:
        if args[:3] == ("/usr/bin/stat", "-c", "%s"):
            return _process(stdout=str(len(bundle)))
        return _process()

    broker.exec.side_effect = broker_exec
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token=token)

    with patch("agent.integrations.modal._create_github_broker", return_value=broker):
        result = backend.execute("git -C /workspace/report fetch origin main --quiet")

    assert result.exit_code == 0
    assert sandbox.filesystem.write_bytes.call_args.args[0] == bundle
    assert all(token not in repr(call) for call in sandbox.exec.call_args_list)
    authenticated_broker_calls = [
        call
        for call in broker.exec.call_args_list
        if "AUTHORIZATION: basic" in repr(call.kwargs.get("env"))
    ]
    assert len(authenticated_broker_calls) == 1
    assert "https://github.com/Nas-Company/nas-reporting.git" in authenticated_broker_calls[0].args
    encoded = (
        authenticated_broker_calls[0]
        .kwargs["env"]["GIT_CONFIG_VALUE_0"]
        .removeprefix("AUTHORIZATION: basic ")
    )
    assert base64.b64decode(encoded).decode() == f"x-access-token:{token}"
    sandbox.rm.assert_called_once()
    broker.terminate.assert_called_once_with()
    broker.detach.assert_called_once_with()


def test_fetch_accepts_explicit_canonical_github_url_for_verified_remote_sha() -> None:
    token = "ghs_repo_scoped"
    sha = "b" * 40
    bundle = b"fetched commit"
    sandbox = MagicMock(object_id="sb-existing")
    broker = MagicMock(object_id="sb-broker")
    broker.filesystem.read_bytes.return_value = bundle
    sandbox.exec.return_value = _process(stdout="updated")

    def broker_exec(*args: str, **_kwargs: object) -> MagicMock:
        if args[:3] == ("/usr/bin/stat", "-c", "%s"):
            return _process(stdout=str(len(bundle)))
        return _process()

    broker.exec.side_effect = broker_exec
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token=token)

    with patch("agent.integrations.modal._create_github_broker", return_value=broker):
        result = backend.execute(
            "git -C /workspace/report fetch "
            f"https://github.com/Nas-Company/nas-reporting.git {sha} --quiet"
        )

    assert result.exit_code == 0
    assert not any("remote get-url" in call.args for call in sandbox.exec.call_args_list)
    authenticated = next(
        call
        for call in broker.exec.call_args_list
        if "AUTHORIZATION: basic" in repr(call.kwargs.get("env"))
    )
    assert "https://github.com/Nas-Company/nas-reporting.git" in authenticated.args
    assert f"{sha}:refs/heads/open-swe-transfer" in authenticated.args


def test_fixed_push_bundles_head_without_token_then_pushes_from_broker() -> None:
    token = "ghs_workflow"
    sha = "a" * 40
    command = (
        "git -C /workspace/report push https://github.com/Nas-Company/nas-reporting.git "
        f"{sha}:refs/heads/report"
    )
    bundle = b"local commits"
    sandbox = MagicMock(object_id="sb-existing")
    broker = MagicMock(object_id="sb-broker")
    sandbox.filesystem.read_bytes.return_value = bundle
    target_responses = iter(
        [
            _process(stdout=f"{sha}\n"),
            _process(),
            _process(),
            _process(stdout=str(len(bundle))),
            _process(),
        ]
    )
    sandbox.exec.side_effect = lambda *_args, **_kwargs: next(target_responses)

    def broker_exec(*args: str, **_kwargs: object) -> MagicMock:
        if "rev-parse" in args:
            return _process(stdout=f"{sha}\n")
        return _process(stdout="pushed")

    broker.exec.side_effect = broker_exec
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="ghs_runtime")
    backend.set_github_token_for_command(token, command)

    with patch("agent.integrations.modal._create_github_broker", return_value=broker):
        result = backend.execute(command)

    assert result.exit_code == 0
    assert all(token not in repr(call) for call in sandbox.exec.call_args_list)
    assert token not in repr(sandbox.filesystem.read_bytes.call_args_list)
    broker.filesystem.write_bytes.assert_called_once_with(bundle, "/broker/input.bundle")
    push_call = next(call for call in broker.exec.call_args_list if "push" in call.args)
    assert push_call.args[-2:] == (
        "https://github.com/Nas-Company/nas-reporting.git",
        "refs/heads/open-swe-source:refs/heads/report",
    )
    encoded = push_call.kwargs["env"]["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")
    assert base64.b64decode(encoded).decode() == f"x-access-token:{token}"
    assert any(
        "update-ref" in call.args and "-d" in call.args for call in sandbox.exec.call_args_list
    )
    sandbox.rm.assert_called_once()
    broker.terminate.assert_called_once_with()
    broker.detach.assert_called_once_with()


def test_local_git_command_does_not_receive_github_token() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    sandbox.exec.return_value = _process(stdout="clean")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="gho_user")

    backend.execute("git status --short; env")

    assert sandbox.exec.call_args.kwargs["env"] is None


def test_gh_token_placeholder_is_removed_before_broker_execution() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    broker = MagicMock(object_id="sb-broker")
    broker.exec.return_value = _process(stdout="ok")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="gho_user")

    with patch("agent.integrations.modal._create_github_broker", return_value=broker):
        backend.execute('GH_TOKEN="${GH_TOKEN:-dummy}" gh repo view')

    sandbox.exec.assert_not_called()
    assert broker.exec.call_args.args == ("/usr/bin/gh", "repo", "view")
    assert broker.exec.call_args.kwargs["env"]["GH_TOKEN"] == "gho_user"


def test_gh_api_literal_fields_remain_available() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    broker = MagicMock(object_id="sb-broker")
    broker.exec.return_value = _process(stdout='{"id": 1}')
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="ghs_app")

    with patch("agent.integrations.modal._create_github_broker", return_value=broker):
        result = backend.execute(
            "gh api repos/Nas-Company/open-swe/issues -F title=test -f body=literal"
        )

    assert result.exit_code == 0
    assert broker.exec.call_args.args == (
        "/usr/bin/gh",
        "api",
        "repos/Nas-Company/open-swe/issues",
        "-F",
        "title=test",
        "-f",
        "body=literal",
    )


def test_plain_github_url_does_not_receive_sandbox_token() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    sandbox.exec.return_value = _process(stdout="ok")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="ghs_app")

    backend.execute("curl -I https://api.github.com/repos/Nas-Company/open-swe")

    assert sandbox.exec.call_args.args == (
        "bash",
        "-c",
        "curl -I https://api.github.com/repos/Nas-Company/open-swe",
    )
    assert sandbox.exec.call_args.kwargs["env"] is None


@pytest.mark.asyncio
async def test_async_execute_uses_refreshed_token_and_can_clear_it() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="old")
    backend.set_github_token("new")

    with patch.object(backend, "_execute_authenticated", return_value=_process()) as execute:
        await backend.aexecute("gh repo view")
    assert execute.call_args.args[1] == "new"
    backend.set_github_token(None)
    sandbox.exec.return_value = _process(stdout="ok")
    backend.execute("pwd")
    assert sandbox.exec.call_args.kwargs["env"] is None


def test_one_shot_command_override_does_not_replace_default_token() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="ghs_runtime")
    command = "gh repo view --repo Nas-Company/open-swe"
    backend.set_github_token_for_command("ghs_workflow", command)

    with patch.object(
        backend, "_execute_authenticated", return_value=_process(stdout="ok")
    ) as execute:
        backend.execute(command)
        backend.execute(command)

    assert execute.call_args_list[0].args[1] == "ghs_workflow"
    assert execute.call_args_list[1].args[1] == "ghs_runtime"


def test_unused_one_shot_override_can_be_revoked() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="ghs_runtime")
    command = "gh repo view --repo Nas-Company/open-swe"
    backend.set_github_token_for_command("ghs_workflow", command)

    assert backend.clear_github_token_for_command("ghs_workflow", command) is True
    assert backend.clear_github_token_for_command("ghs_workflow", command) is False

    with patch.object(
        backend, "_execute_authenticated", return_value=_process(stdout="ok")
    ) as execute:
        backend.execute(command)

    assert execute.call_args.args[1] == "ghs_runtime"


def test_authenticated_output_and_exception_redact_token_encodings() -> None:
    token = "ghs_do_not_expose"
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    sandbox = MagicMock(object_id="sb-existing")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token=token)

    with patch.object(
        backend,
        "_execute_authenticated",
        return_value=_process(stdout=f"bearer={token} basic={credentials}"),
    ):
        response = backend.execute("gh repo view")
    assert token not in response.output
    assert credentials not in response.output

    with patch.object(
        backend,
        "_execute_authenticated",
        side_effect=RuntimeError(f"request contained {token} and {credentials}"),
    ):
        response = backend.execute("gh repo view")
    assert token not in response.output
    assert credentials not in response.output


def test_identical_one_shot_overrides_are_consumed_fifo() -> None:
    sandbox = MagicMock(object_id="sb-existing")
    backend = AuthenticatedModalSandbox(sandbox=sandbox, github_token="ghs_runtime")
    command = "gh repo view --repo Nas-Company/open-swe"
    backend.set_github_token_for_command("ghs_first", command)
    backend.set_github_token_for_command("ghs_second", command)

    with patch.object(
        backend, "_execute_authenticated", return_value=_process(stdout="ok")
    ) as execute:
        backend.execute(command)
        backend.execute(command)

    assert execute.call_args_list[0].args[1] == "ghs_first"
    assert execute.call_args_list[1].args[1] == "ghs_second"


def test_sandbox_factory_passes_token_only_to_modal() -> None:
    module = MagicMock()
    module.create_modal_sandbox.return_value = MagicMock(id="modal")
    with (
        patch("agent.utils.sandbox.import_module", return_value=module),
        patch.dict(os.environ, {"SANDBOX_TYPE": "modal"}),
    ):
        sandbox = create_sandbox("existing", github_token="ghs_token")

    assert sandbox.id == "modal"
    module.create_modal_sandbox.assert_called_once_with(
        "existing",
        github_token="ghs_token",
    )

    module.reset_mock()
    module.create_langsmith_sandbox.return_value = MagicMock(id="langsmith")
    with (
        patch("agent.utils.sandbox.import_module", return_value=module),
        patch.dict(os.environ, {"SANDBOX_TYPE": "langsmith"}),
    ):
        sandbox = create_sandbox(
            "existing",
            snapshot_id="snapshot",
            github_token="ghs_token",
        )

    assert sandbox.id == "langsmith"
    module.create_langsmith_sandbox.assert_called_once_with(
        "existing",
        snapshot_id="snapshot",
    )


def test_proxy_refreshes_only_the_current_backend() -> None:
    old_backend = MagicMock(id="old")
    new_backend = MagicMock(id="new")
    proxy = SandboxBackendProxy(old_backend)

    proxy.set_github_token("old-token")
    proxy.replace_backend(new_backend)
    proxy.set_github_token("fresh-token")
    proxy.set_github_token_for_command("workflow-token", "git push origin main")
    new_backend.clear_github_token_for_command.return_value = True
    assert proxy.clear_github_token_for_command("workflow-token", "git push origin main") is True

    old_backend.set_github_token.assert_called_once_with("old-token")
    new_backend.set_github_token.assert_called_once_with("fresh-token")
    new_backend.set_github_token_for_command.assert_called_once_with(
        "workflow-token",
        "git push origin main",
    )
    new_backend.clear_github_token_for_command.assert_called_once_with(
        "workflow-token",
        "git push origin main",
    )
