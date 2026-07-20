from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from deepagents.middleware.skills import _parse_skill_metadata

from agent import analyzer
from agent.dashboard import review_style_jobs
from agent.dashboard.review_style_jobs import (
    build_continual_run_configurable,
    build_continual_run_input,
)
from agent.review_style_collector import ReviewSample, ReviewStyleSamples
from agent.utils.analyzer_skills import (
    ANALYZER_MODES,
    SKILLS_DIR,
    build_skill_files,
    skill_path_for_mode,
)


def test_build_skill_files_stripped_keys_and_valid_file_data() -> None:
    files = build_skill_files()
    assert set(files) == {
        "/bootstrap-repo-analysis/SKILL.md",
        "/continual-learning/SKILL.md",
    }
    for entry in files.values():
        assert entry["encoding"] == "utf-8"
        assert isinstance(entry["content"], str) and entry["content"].strip()
        assert "created_at" in entry and "modified_at" in entry


def test_skill_path_for_mode() -> None:
    assert skill_path_for_mode("bootstrap") == "/skills/bootstrap-repo-analysis/SKILL.md"
    assert skill_path_for_mode("continual") == "/skills/continual-learning/SKILL.md"
    # Unknown modes fall back to bootstrap.
    assert skill_path_for_mode("whatever") == "/skills/bootstrap-repo-analysis/SKILL.md"


def test_bundled_skill_md_parse() -> None:
    for skill in ANALYZER_MODES.values():
        path = SKILLS_DIR / skill / "SKILL.md"
        meta = _parse_skill_metadata(path.read_text(), str(path), path.parent.name)
        assert meta["name"] == skill
        assert meta["description"].strip()


def test_skills_dir_resolves() -> None:
    assert SKILLS_DIR.name == "skills"
    assert (SKILLS_DIR / "bootstrap-repo-analysis" / "SKILL.md").exists()
    assert isinstance(SKILLS_DIR, pathlib.Path)


def test_continual_run_payload_carries_mode_and_skill_files() -> None:
    configurable = build_continual_run_configurable("o/r")
    assert configurable["analyzer_mode"] == "continual"
    assert configurable["review_style_full_name"] == "o/r"
    assert configurable.get("thread_id")

    run_input = build_continual_run_input("o/r")
    assert "/continual-learning/SKILL.md" in run_input["files"]
    assert run_input["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_analyzer_sandbox_uses_repo_scoped_app_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_backend = MagicMock()
    ensure_sandbox = AsyncMock(return_value=sandbox_backend)
    monkeypatch.setattr(analyzer, "ensure_sandbox_for_thread", ensure_sandbox)
    monkeypatch.setattr(
        analyzer,
        "aresolve_sandbox_work_dir",
        AsyncMock(return_value="/workspace"),
    )
    monkeypatch.setattr(analyzer, "CompositeBackend", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(analyzer, "provider_model_kwargs", MagicMock(return_value={}))
    monkeypatch.setattr(analyzer, "make_model", MagicMock(return_value=MagicMock()))
    graph = MagicMock()
    graph.with_config.return_value = graph
    monkeypatch.setattr(analyzer, "create_deep_agent", MagicMock(return_value=graph))

    config = {
        "configurable": {
            "thread_id": "analyzer-thread",
            "__is_for_execution__": True,
            "review_style_full_name": "Nas-Company/nas-reporting",
        }
    }

    result = await analyzer.get_analyzer(config)

    assert result is graph
    ensure_sandbox.assert_awaited_once_with(
        "analyzer-thread",
        github_proxy_repositories=["nas-reporting"],
        github_proxy_permissions=analyzer.READ_ONLY_SANDBOX_TOKEN_PERMISSIONS,
        repo={"owner": "Nas-Company", "name": "nas-reporting"},
    )
    assert "github_proxy_token" not in ensure_sandbox.await_args.kwargs


@pytest.mark.asyncio
async def test_bootstrap_run_config_does_not_persist_raw_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_token = "gho_must_remain_server_side"
    samples = ReviewStyleSamples(
        full_name="Nas-Company/nas-reporting",
        owner="Nas-Company",
        name="nas-reporting",
        top_reviewers=["reviewer"],
        samples=[ReviewSample(1, "reviewer", "review", "Check the report output.")],
        prs_scanned=1,
        reviews_scanned=1,
    )
    collect_samples = AsyncMock(return_value=samples)
    client = MagicMock()
    client.runs.create = AsyncMock(return_value={"run_id": "run-1"})
    monkeypatch.setattr(review_style_jobs, "collect_review_samples", collect_samples)
    monkeypatch.setattr(review_style_jobs, "_client", lambda: client)
    monkeypatch.setattr(review_style_jobs, "mark_analysis_running", AsyncMock())
    monkeypatch.setattr(
        review_style_jobs,
        "update_review_style",
        AsyncMock(return_value={"status": "running"}),
    )

    await review_style_jobs.start_bootstrap_analysis(
        "Nas-Company/nas-reporting",
        github_token=raw_token,
        created_by="xiaoqi",
    )

    collect_samples.assert_awaited_once_with(raw_token, "Nas-Company", "nas-reporting")
    run_call = client.runs.create.await_args
    configurable = run_call.kwargs["config"]["configurable"]
    assert "review_style_github_token" not in configurable
    assert raw_token not in repr(run_call)
