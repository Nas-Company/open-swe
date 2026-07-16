from __future__ import annotations

from deepagents.backends.protocol import ExecuteResponse

from agent.utils.repo_prep import materialize_trusted_skills, prepare_review_repo


class _FakeSandboxBackend:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        raise_exc: bool = False,
        output: str = "",
        outputs: list[str] | None = None,
        results: list[tuple[int, str]] | None = None,
        reject_compound_auth: bool = False,
    ) -> None:
        self._exit_code = exit_code
        self._raise = raise_exc
        self._output = output
        self._outputs = outputs
        self._results = results
        self._reject_compound_auth = reject_compound_auth
        self.commands: list[str] = []

    @property
    def id(self) -> str:
        return "fake-sandbox"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        del timeout
        if self._raise:
            raise RuntimeError("sandbox unreachable")
        if self._reject_compound_auth and 'GH_TOKEN="${GH_TOKEN:-dummy}"' in command:
            authenticated_command = command.split(" && ", 1)[-1]
            assert " && " not in authenticated_command
            assert "||" not in authenticated_command
            assert ";" not in authenticated_command
            assert "|" not in authenticated_command
            assert "$(" not in authenticated_command
            assert "\n" not in authenticated_command
        if self._reject_compound_auth and "git clone" in command:
            assert "git clone --no-checkout" in command
        self.commands.append(command)
        exit_code = self._exit_code
        output = self._output
        if self._results is not None:
            exit_code, output = self._results[len(self.commands) - 1]
        if self._outputs is not None:
            output = self._outputs[len(self.commands) - 1]
        return ExecuteResponse(output=output, exit_code=exit_code, truncated=False)


async def test_prepare_review_repo_clones_and_checks_out_head() -> None:
    backend = _FakeSandboxBackend(
        results=[
            (1, ""),
            (0, ""),
            (0, ""),
            (0, ""),
            (0, ""),
            (0, ""),
            (0, "abc123\n"),
        ],
        reject_compound_auth=True,
    )
    ok = await prepare_review_repo(
        backend,
        work_dir="/work",
        repo_owner="acme",
        repo_name="widget",
        head_sha="abc123",
        pr_number=42,
        base_sha="def456",
    )
    assert ok is True
    assert backend.commands == [
        "test -d /work/widget/.git",
        "cd /work && git clone --no-checkout --quiet "
        "https://github.com/acme/widget.git /work/widget",
        'cd /work/widget && GH_TOKEN="${GH_TOKEN:-dummy}" git fetch origin def456 --quiet',
        'cd /work/widget && GH_TOKEN="${GH_TOKEN:-dummy}" git fetch origin abc123 --quiet',
        'cd /work/widget && GH_TOKEN="${GH_TOKEN:-dummy}" '
        "git fetch origin refs/pull/42/head --quiet",
        "git -C /work/widget checkout --force abc123 --quiet",
        "git -C /work/widget rev-parse HEAD",
    ]


async def test_prepare_review_repo_fetches_existing_repo_with_isolated_auth_commands() -> None:
    backend = _FakeSandboxBackend(
        results=[
            (0, ""),
            (0, ""),
            (0, ""),
            (0, ""),
            (0, "abc123\n"),
        ],
        reject_compound_auth=True,
    )
    ok = await prepare_review_repo(
        backend,
        work_dir="/work",
        repo_owner="acme",
        repo_name="widget",
        head_sha="abc123",
    )
    assert ok is True
    assert backend.commands == [
        "test -d /work/widget/.git",
        'cd /work/widget && GH_TOKEN="${GH_TOKEN:-dummy}" git fetch origin --quiet',
        'cd /work/widget && GH_TOKEN="${GH_TOKEN:-dummy}" git fetch origin abc123 --quiet',
        "git -C /work/widget checkout --force abc123 --quiet",
        "git -C /work/widget rev-parse HEAD",
    ]


async def test_prepare_review_repo_skips_pull_ref_without_pr_number() -> None:
    backend = _FakeSandboxBackend(results=[(1, ""), (0, ""), (0, ""), (0, ""), (0, "abc123\n")])
    ok = await prepare_review_repo(
        backend,
        work_dir="/work",
        repo_owner="acme",
        repo_name="widget",
        head_sha="abc123",
    )
    assert ok is True
    assert all("refs/pull" not in command for command in backend.commands)


async def test_prepare_review_repo_skips_checkout_without_head() -> None:
    backend = _FakeSandboxBackend(results=[(1, ""), (0, "")])
    ok = await prepare_review_repo(
        backend,
        work_dir="/work",
        repo_owner="acme",
        repo_name="widget",
        head_sha="",
    )
    assert ok is True
    assert all("git checkout" not in command for command in backend.commands)


async def test_prepare_review_repo_requires_owner_and_name() -> None:
    backend = _FakeSandboxBackend()
    ok = await prepare_review_repo(
        backend, work_dir="/work", repo_owner="", repo_name="widget", head_sha="abc"
    )
    assert ok is False
    assert backend.commands == []


async def test_prepare_review_repo_returns_false_on_nonzero_exit() -> None:
    backend = _FakeSandboxBackend(exit_code=1)
    ok = await prepare_review_repo(
        backend, work_dir="/work", repo_owner="acme", repo_name="widget", head_sha="abc"
    )
    assert ok is False


async def test_prepare_review_repo_returns_false_on_exception() -> None:
    backend = _FakeSandboxBackend(raise_exc=True)
    ok = await prepare_review_repo(
        backend, work_dir="/work", repo_owner="acme", repo_name="widget", head_sha="abc"
    )
    assert ok is False


async def test_materialize_trusted_skills_extracts_from_trusted_ref() -> None:
    backend = _FakeSandboxBackend(outputs=["/work/.review-skills/.agents/skills\n", ""])
    sources = await materialize_trusted_skills(
        backend, repo_dir="/work/widget", trusted_ref="def456"
    )
    assert sources == ["/work/.review-skills/.agents/skills/"]
    assert len(backend.commands) == 2
    for cmd in backend.commands:
        assert "git cat-file -e def456:" in cmd
        assert "git archive def456" in cmd


async def test_materialize_trusted_skills_empty_without_ref() -> None:
    backend = _FakeSandboxBackend()
    sources = await materialize_trusted_skills(backend, repo_dir="/work/widget", trusted_ref="")
    assert sources == []
    assert backend.commands == []


async def test_materialize_trusted_skills_empty_when_none_exist() -> None:
    backend = _FakeSandboxBackend(output="")
    sources = await materialize_trusted_skills(
        backend, repo_dir="/work/widget", trusted_ref="def456"
    )
    assert sources == []


async def test_materialize_trusted_skills_handles_exception() -> None:
    backend = _FakeSandboxBackend(raise_exc=True)
    sources = await materialize_trusted_skills(
        backend, repo_dir="/work/widget", trusted_ref="def456"
    )
    assert sources == []
