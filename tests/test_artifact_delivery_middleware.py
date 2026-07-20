from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from agent.middleware.artifact_delivery import (
    _LIST_OUTBOX_SCRIPT,
    ArtifactDeliveryMiddleware,
    extract_artifact_paths,
)

WORK_DIR = "/workspace"
REPO_DIR = "/workspace/nas-reporting"


class _BoundFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: object, **kwargs: object) -> _BoundFakeModel:  # noqa: ARG002
        return self


def _middleware() -> ArtifactDeliveryMiddleware:
    return ArtifactDeliveryMiddleware(
        work_dir=WORK_DIR,
        repo_dir=REPO_DIR,
        outbox_enabled=False,
    )


async def _after_model(
    messages: list[HumanMessage | AIMessage | ToolMessage],
    **state_updates: object,
) -> dict[str, object] | None:
    state: dict[str, object] = {"messages": messages, **state_updates}
    return await _middleware().aafter_model(state, MagicMock())


def _injected_calls(result: dict[str, object] | None) -> list[dict[str, object]]:
    assert result is not None
    messages = result.get("messages")
    assert isinstance(messages, list)
    assert len(messages) == 1
    message = messages[0]
    assert isinstance(message, AIMessage)
    return message.tool_calls


def _injected_paths(result: dict[str, object] | None) -> list[str]:
    calls = _injected_calls(result)
    assert calls
    assert all(call["name"] == "publish_artifact" for call in calls)
    assert all(call["type"] == "tool_call" for call in calls)
    return [str(call["args"]["file_path"]) for call in calls]


def test_extract_artifact_paths_maps_repo_relative_deliverables() -> None:
    text = (
        "### Deliverables\nGenerated `examples/leverage-edu-report.html` and "
        "`examples/leverage-edu-report-spec.json`."
    )

    assert extract_artifact_paths(text, work_dir=WORK_DIR, repo_dir=REPO_DIR) == [
        "/workspace/nas-reporting/examples/leverage-edu-report.html",
        "/workspace/nas-reporting/examples/leverage-edu-report-spec.json",
    ]


@pytest.mark.asyncio
async def test_final_text_injects_publish_calls_for_html_and_json() -> None:
    final = AIMessage(
        content=("### Deliverables\n`examples/report.html` and `examples/report-spec.json`.")
    )

    result = await _after_model([HumanMessage(content="Generate the report"), final])

    assert _injected_paths(result) == [
        "/workspace/nas-reporting/examples/report.html",
        "/workspace/nas-reporting/examples/report-spec.json",
    ]
    assert final.tool_calls == _injected_calls(result)
    assert len({str(call["id"]) for call in final.tool_calls}) == 2


@pytest.mark.asyncio
async def test_sandbox_absolute_path_is_preserved() -> None:
    final = AIMessage(
        content=(
            "Open the finished report at "
            "[`report.html`](sandbox:/workspace/nas-reporting/examples/report.html)."
        )
    )

    result = await _after_model([HumanMessage(content="Generate it"), final])

    assert _injected_paths(result) == [
        "/workspace/nas-reporting/examples/report.html",
    ]


@pytest.mark.asyncio
async def test_sandbox_path_decodes_spaces_and_unicode() -> None:
    final = AIMessage(
        content=(
            "Download the report from "
            "[result](sandbox:/workspace/nas-reporting/examples/final%20报告.html)."
        )
    )

    result = await _after_model([HumanMessage(content="Generate it"), final])

    assert _injected_paths(result) == [
        "/workspace/nas-reporting/examples/final 报告.html",
    ]


@pytest.mark.asyncio
async def test_duplicate_candidate_spellings_inject_one_call_per_file() -> None:
    final = AIMessage(
        content=(
            "### Deliverables\nFinal HTML: `examples/report.html`; again "
            "`/workspace/nas-reporting/examples/report.html`; and "
            "[`download`](sandbox:/workspace/nas-reporting/examples/report.html). "
            "Spec: `examples/report-spec.json`, repeated `examples/report-spec.json`."
        )
    )

    result = await _after_model([HumanMessage(content="Generate it"), final])

    assert _injected_paths(result) == [
        "/workspace/nas-reporting/examples/report.html",
        "/workspace/nas-reporting/examples/report-spec.json",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        "src/report.ts",
        "package.json",
        ".gitignore",
        "qa/screenshots/report.png",
        "/workspace/.open-swe/attachments/source-analysis.md",
        "credentials.json",
        ".aws/credentials",
    ],
)
async def test_non_deliverable_or_sensitive_paths_are_excluded(candidate: str) -> None:
    final = AIMessage(content=f"Generated `{candidate}`.")

    result = await _after_model([HumanMessage(content="Generate it"), final])

    assert result is None


@pytest.mark.asyncio
async def test_already_attempted_publish_path_is_not_retried() -> None:
    attempted = AIMessage(
        content="Publishing the report.",
        tool_calls=[
            {
                "name": "publish_artifact",
                "args": {"file_path": "/workspace/nas-reporting/examples/report.html"},
                "id": "publish-1",
                "type": "tool_call",
            }
        ],
    )
    failed = ToolMessage(
        content="Artifact publication failed.",
        name="publish_artifact",
        tool_call_id="publish-1",
    )
    final = AIMessage(content="### Deliverables\n`examples/report.html`.")

    result = await _after_model([HumanMessage(content="Generate it"), attempted, failed, final])

    assert result is None


@pytest.mark.asyncio
async def test_failed_workspace_relative_publish_is_retried_with_repo_path() -> None:
    attempted = AIMessage(
        content="Publishing the report.",
        tool_calls=[
            {
                "name": "publish_artifact",
                "args": {"file_path": "examples/report.html"},
                "id": "publish-1",
                "type": "tool_call",
            }
        ],
    )
    failed = ToolMessage(
        content="file must be inside the sandbox workspace",
        name="publish_artifact",
        tool_call_id="publish-1",
    )
    final = AIMessage(content="### Deliverables\n`examples/report.html`.")

    result = await _after_model([HumanMessage(content="Generate it"), attempted, failed, final])

    assert _injected_paths(result) == [
        "/workspace/nas-reporting/examples/report.html",
    ]


@pytest.mark.asyncio
async def test_only_unattempted_candidate_is_injected() -> None:
    attempted = AIMessage(
        content="Publishing the HTML.",
        tool_calls=[
            {
                "name": "publish_artifact",
                "args": {"file_path": "/workspace/nas-reporting/examples/report.html"},
                "id": "publish-1",
                "type": "tool_call",
            }
        ],
    )
    published = ToolMessage(
        content="Published.",
        name="publish_artifact",
        tool_call_id="publish-1",
    )
    final = AIMessage(
        content=("### Deliverables\n`examples/report.html` and `examples/report-spec.json`.")
    )

    result = await _after_model([HumanMessage(content="Generate it"), attempted, published, final])

    assert _injected_paths(result) == [
        "/workspace/nas-reporting/examples/report-spec.json",
    ]


@pytest.mark.asyncio
async def test_model_tool_calls_are_not_overwritten() -> None:
    final = AIMessage(
        content="I will validate `examples/report.html` now.",
        tool_calls=[
            {
                "name": "execute",
                "args": {"command": "test -f examples/report.html"},
                "id": "execute-1",
                "type": "tool_call",
            }
        ],
    )

    result = await _after_model([HumanMessage(content="Generate it"), final])

    assert result is None
    assert [call["name"] for call in final.tool_calls] == ["execute"]


@pytest.mark.asyncio
async def test_generic_changed_files_are_not_treated_as_deliverables() -> None:
    final = AIMessage(
        content=(
            "## Files changed\n"
            "- Updated `config/customer-data.json`.\n"
            "- Fixed profile loading in `config/profile.json`.\n"
            "- Documented `README.md`."
        )
    )

    result = await _after_model([HumanMessage(content="Fix the bug"), final])

    assert result is None


@pytest.mark.asyncio
async def test_plan_mode_does_not_publish() -> None:
    final = AIMessage(content="Proposed output: `examples/report.html`.")

    result = await _after_model(
        [HumanMessage(content="Plan the work"), final],
        plan_mode=True,
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_text",
    [
        "Model call limits exceeded: run limit reached. `examples/report.html`",
        ("Sandbox circuit breaker triggered: 3 consecutive failures. `examples/report.html`"),
    ],
)
async def test_incomplete_terminal_states_do_not_publish(terminal_text: str) -> None:
    final = AIMessage(content=terminal_text)

    result = await _after_model([HumanMessage(content="Generate it"), final])

    assert result is None


@pytest.mark.asyncio
async def test_controlled_outbox_publishes_without_final_path_text() -> None:
    class Backend:
        async def aexecute(self, command: str, *, timeout: int | None = None) -> object:
            assert "deliverables" in command
            assert timeout == 30
            return SimpleNamespace(
                exit_code=0,
                truncated=False,
                output=json.dumps(
                    [
                        "/workspace/.open-swe/deliverables/report.html",
                        "/workspace/.open-swe/deliverables/credentials.json",
                        "/workspace/.open-swe/deliverables/.private/report.html",
                    ]
                ),
            )

    middleware = ArtifactDeliveryMiddleware(
        work_dir=WORK_DIR,
        repo_dir=REPO_DIR,
        sandbox_backend=Backend(),
    )
    final = AIMessage(content="Completed successfully.")

    result = await middleware.aafter_model(
        {"messages": [HumanMessage(content="Generate it"), final]}, MagicMock()
    )

    assert _injected_paths(result) == [
        "/workspace/.open-swe/deliverables/report.html",
    ]


def test_outbox_candidates_must_remain_inside_controlled_directory() -> None:
    assert (
        extract_artifact_paths(
            "Completed successfully.",
            work_dir=WORK_DIR,
            repo_dir=REPO_DIR,
            outbox_paths=("/workspace/nas-reporting/config/customer-data.json",),
        )
        == []
    )


def test_outbox_listing_rejects_symlink_root(tmp_path: Path) -> None:
    root = tmp_path
    source = root / "source"
    source.mkdir()
    (source / "customer-data.json").write_text('{"sensitive": true}')
    outbox = root / "deliverables"
    outbox.symlink_to(source, target_is_directory=True)

    result = subprocess.run(
        [sys.executable, "-c", _LIST_OUTBOX_SCRIPT, str(outbox)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == []


@pytest.mark.asyncio
async def test_outbox_inspection_failure_does_not_block_terminal_response() -> None:
    class Backend:
        async def aexecute(self, command: str, *, timeout: int | None = None) -> object:
            raise RuntimeError("sandbox unavailable")

    middleware = ArtifactDeliveryMiddleware(
        work_dir=WORK_DIR,
        repo_dir=REPO_DIR,
        sandbox_backend=Backend(),
    )
    final = AIMessage(content="Completed successfully.")

    result = await middleware.aafter_model(
        {"messages": [HumanMessage(content="Generate it"), final]}, MagicMock()
    )

    assert result is None


@pytest.mark.asyncio
async def test_injected_publish_call_executes_through_compiled_tool_node() -> None:
    published: list[str] = []

    @tool
    def publish_artifact(file_path: str) -> dict[str, object]:
        """Publish one artifact."""
        published.append(file_path)
        return {"success": True, "downloadUrl": "/download/x"}

    model = _BoundFakeModel(
        responses=[
            AIMessage(content="## Deliverables\n- `examples/report.html`"),
            AIMessage(content="Download: /download/x"),
        ]
    )
    graph = create_agent(model, tools=[publish_artifact], middleware=[_middleware()])

    result = await graph.ainvoke({"messages": [HumanMessage(content="Make report")]})

    assert published == ["/workspace/nas-reporting/examples/report.html"]
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "Download: /download/x"
