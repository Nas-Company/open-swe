from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.tools import StructuredTool

from agent import server
from agent.integrations import live_issue_mcp


def _fake_tool(name: str, response: object | None = None) -> StructuredTool:
    async def fake(**_kwargs):
        """Fake MCP tool."""
        return response if response is not None else {"ok": True}

    return StructuredTool.from_function(
        coroutine=fake,
        name=name,
        description=f"Fake {name}",
    )


@pytest.fixture(autouse=True)
def clear_live_issue_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LIIS_TOKEN",
        "LIVE_ISSUE_INVESTIGATION_TOKEN",
        "LIIS_MCP_TOKEN",
        "LIVE_ISSUE_MCP_TOKEN",
        "LIIS_MCP_URL",
        "LIVE_ISSUE_INVESTIGATION_MCP_URL",
        "LIVE_ISSUE_MCP_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_load_live_issue_mcp_config_empty_without_token() -> None:
    assert live_issue_mcp.load_live_issue_mcp_config() is None


def test_load_live_issue_mcp_config_uses_default_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIIS_TOKEN", "tok")

    config = live_issue_mcp.load_live_issue_mcp_config()

    assert config == live_issue_mcp.LiveIssueMCPConfig(
        url=live_issue_mcp.DEFAULT_LIVE_ISSUE_MCP_URL,
        token="tok",
    )


def test_load_live_issue_mcp_config_accepts_query_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LIIS_MCP_URL",
        "https://liveissueinvestigationservice.dev-nas.com/mcp?token=tok",
    )

    config = live_issue_mcp.load_live_issue_mcp_config()

    assert config == live_issue_mcp.LiveIssueMCPConfig(
        url=live_issue_mcp.DEFAULT_LIVE_ISSUE_MCP_URL,
        token="tok",
    )


def test_load_live_issue_mcp_config_accepts_local_dev_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIIS_TOKEN", "tok")
    monkeypatch.setenv("LIIS_MCP_URL", "http://localhost:4111/mcp")

    assert live_issue_mcp.load_live_issue_mcp_config() == live_issue_mcp.LiveIssueMCPConfig(
        url="http://localhost:4111/mcp",
        token="tok",
    )


def test_load_live_issue_mcp_config_rejects_non_liis_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIIS_TOKEN", "tok")
    monkeypatch.setenv("LIIS_MCP_URL", "https://example.com/mcp")

    assert live_issue_mcp.load_live_issue_mcp_config() is None


@pytest.mark.asyncio
async def test_load_live_issue_tools_empty_when_not_configured() -> None:
    assert await live_issue_mcp.load_live_issue_tools() == []


@pytest.mark.asyncio
async def test_load_live_issue_tools_degrades_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIIS_TOKEN", "tok")

    with patch.object(
        live_issue_mcp,
        "_build_mcp_tools",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        assert await live_issue_mcp.load_live_issue_tools() == []


@pytest.mark.asyncio
async def test_load_live_issue_tools_filters_to_allowed_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIIS_TOKEN", "tok")
    lark_context = _fake_tool("get_lark_thread_context")
    code_search = _fake_tool("search_code")
    other_tool = _fake_tool("delete_production_data")

    with patch.object(
        live_issue_mcp,
        "_build_mcp_tools",
        AsyncMock(return_value=[other_tool, lark_context, code_search]),
    ):
        tools = await live_issue_mcp.load_live_issue_tools()

    assert [tool.name for tool in tools] == ["get_lark_thread_context", "search_code"]


@pytest.mark.asyncio
async def test_load_live_issue_tools_redacts_sensitive_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIIS_TOKEN", "tok")
    aws_tool = _fake_tool(
        "list_aws_resources",
        {
            "Environment": {
                "Variables": {
                    "MONGO_URI": "mongodb+srv://user:pass@example/db",
                    "ROUTE_API_KEY": "secret-key",
                    "SAFE_NAME": "still-redacted-in-environment",
                }
            },
            "message": "Bearer abc.def.ghi https://open.larksuite.com/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000 sk-abcdefghijklmnopqrstuvwx",
        },
    )

    with patch.object(
        live_issue_mcp,
        "_build_mcp_tools",
        AsyncMock(return_value=[aws_tool]),
    ):
        tools = await live_issue_mcp.load_live_issue_tools()

    result = await tools[0].ainvoke({})

    assert result["Environment"]["Variables"] == {
        "MONGO_URI": "<redacted>",
        "ROUTE_API_KEY": "<redacted>",
        "SAFE_NAME": "<redacted>",
    }
    assert "mongodb+srv://" not in result["message"]
    assert "sk-abcdefghijklmnopqrstuvwx" not in result["message"]
    assert "Bearer <redacted>" in result["message"]


@pytest.mark.asyncio
async def test_load_live_issue_tools_redacts_json_text_content_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIIS_TOKEN", "tok")
    aws_tool = _fake_tool(
        "list_aws_resources",
        [
            {
                "type": "text",
                "text": '{"Environment":{"Variables":{"ROUTE_API_KEY":"secret","SAFE_NAME":"secret"}}}',
            }
        ],
    )

    with patch.object(
        live_issue_mcp,
        "_build_mcp_tools",
        AsyncMock(return_value=[aws_tool]),
    ):
        tools = await live_issue_mcp.load_live_issue_tools()

    result = await tools[0].ainvoke({})
    text_payload = json.loads(result[0]["text"])

    assert text_payload["Environment"]["Variables"] == {
        "ROUTE_API_KEY": "<redacted>",
        "SAFE_NAME": "<redacted>",
    }


@pytest.mark.asyncio
async def test_server_load_live_issue_mcp_tools_skipped_when_unauthorized() -> None:
    with patch.object(server, "load_live_issue_tools", AsyncMock(return_value=["liis"])):
        assert await server._load_live_issue_mcp_tools(authorized=False) == []


@pytest.mark.asyncio
async def test_server_load_live_issue_mcp_tools_loaded_when_authorized() -> None:
    with patch.object(server, "load_live_issue_tools", AsyncMock(return_value=["liis"])):
        assert await server._load_live_issue_mcp_tools(authorized=True) == ["liis"]


@pytest.mark.asyncio
async def test_server_load_live_issue_mcp_tools_degrades_on_error() -> None:
    with patch.object(
        server,
        "load_live_issue_tools",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        assert await server._load_live_issue_mcp_tools(authorized=True) == []


@pytest.mark.asyncio
async def test_get_agent_passes_live_issue_prompt_state() -> None:
    config = {
        "configurable": {
            "__is_for_execution__": True,
            "thread_id": "thread-123",
        },
        "metadata": {},
    }

    def fake_create_deep_agent(**_kwargs):
        class _DummyAgent:
            def with_config(self, _config):
                return self

        return _DummyAgent()

    async def run_with_live_issue_tools(live_issue_tools: list[object]) -> bool:
        with (
            patch.object(
                server,
                "resolve_github_token",
                new_callable=AsyncMock,
                return_value=("ghp", None),
            ),
            patch.object(server, "resolve_triggering_user_identity", return_value=None),
            patch.object(
                server,
                "ensure_sandbox_for_thread",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                server,
                "aresolve_sandbox_work_dir",
                new_callable=AsyncMock,
                return_value="/workspace",
            ),
            patch.object(
                server,
                "get_team_default_model_pair",
                new_callable=AsyncMock,
                return_value=(("openai:gpt-5.5", "medium"), ("openai:gpt-5.5", "low")),
            ),
            patch.object(server, "fallback_model_id_for", return_value=None),
            patch.object(server, "make_model", return_value=MagicMock()),
            patch.object(
                server,
                "_observability_authorized",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                server, "_load_observability_tools", new_callable=AsyncMock, return_value=[]
            ),
            patch.object(
                server,
                "_load_live_issue_mcp_tools",
                new_callable=AsyncMock,
                return_value=live_issue_tools,
            ),
            patch.object(
                server,
                "_load_corridor_mcp_tools",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(server, "construct_system_prompt", return_value="prompt") as prompt,
            patch.object(server, "create_deep_agent", side_effect=fake_create_deep_agent),
        ):
            await server.get_agent(config)
        return bool(prompt.call_args.kwargs["live_issue_enabled"])

    assert await run_with_live_issue_tools([]) is False
    assert await run_with_live_issue_tools([_fake_tool("get_lark_thread_context")]) is True
