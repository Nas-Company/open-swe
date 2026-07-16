from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from agent.middleware.codex_proxy_tool_images import CodexProxyToolImageMiddleware
from agent.server import _general_purpose_subagent


def _tool_call(call_id: str, name: str = "read_file") -> dict[str, object]:
    return {"name": name, "args": {}, "id": call_id, "type": "tool_call"}


def _codex_model(monkeypatch: pytest.MonkeyPatch) -> ChatOpenAI:
    monkeypatch.setenv("CODEX_PROXY_BASE_URL", "https://codex-proxy.example/v1")
    return ChatOpenAI(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://codex-proxy.example/v1",
        use_responses_api=False,
    )


def _image(base64: str, mime_type: str) -> dict[str, str]:
    return {"type": "image", "base64": base64, "mime_type": mime_type}


def test_bridges_parallel_tool_images_and_serializes_as_user_image_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _codex_model(monkeypatch)
    assistant = AIMessage(
        content="",
        tool_calls=[_tool_call("call_1"), _tool_call("call_2")],
    )
    first_image = _image("Zmlyc3Q=", "image/png")
    second_image = _image("c2Vjb25k", "image/jpeg")
    first_tool = ToolMessage(
        content=[{"type": "text", "text": "desktop"}, first_image],
        tool_call_id="call_1",
        name="read_file",
    )
    second_tool = ToolMessage(
        content=[second_image],
        tool_call_id="call_2",
        name="read_file",
    )
    original_messages = [HumanMessage(content="inspect"), assistant, first_tool, second_tool]
    request = ModelRequest(model=model, messages=original_messages)
    captured: list[ModelRequest] = []
    response = MagicMock()

    def handler(outgoing: ModelRequest) -> MagicMock:
        captured.append(outgoing)
        return response

    result = CodexProxyToolImageMiddleware().wrap_model_call(request, handler)

    assert result is response
    assert request.messages is original_messages
    assert first_tool.content == [{"type": "text", "text": "desktop"}, first_image]
    assert second_tool.content == [second_image]

    outgoing = captured[0]
    assert outgoing is not request
    assert len(outgoing.messages) == 5
    assert isinstance(outgoing.messages[-1], HumanMessage)
    outgoing_tools = outgoing.messages[2:4]
    assert all(isinstance(message, ToolMessage) for message in outgoing_tools)
    assert all(
        not any(block.get("type") == "image" for block in message.content)
        for message in outgoing_tools
    )

    payload = model._get_request_payload(outgoing.messages)  # noqa: SLF001
    assert [message["role"] for message in payload["messages"][-4:]] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    user_blocks = payload["messages"][-1]["content"]
    assert "untrusted artifact content" in user_blocks[0]["text"]
    image_urls = [
        block["image_url"]["url"] for block in user_blocks if block["type"] == "image_url"
    ]
    assert image_urls == [
        "data:image/png;base64,Zmlyc3Q=",
        "data:image/jpeg;base64,c2Vjb25k",
    ]


@pytest.mark.asyncio
async def test_async_bridge_uses_request_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _codex_model(monkeypatch)
    tool = ToolMessage(
        content=[_image("aW1hZ2U=", "image/png")],
        tool_call_id="call_1",
    )
    request = ModelRequest(
        model=model,
        messages=[AIMessage(content="", tool_calls=[_tool_call("call_1")]), tool],
    )
    response = MagicMock()

    async def handler(outgoing: ModelRequest) -> MagicMock:
        assert outgoing is not request
        assert isinstance(outgoing.messages[-1], HumanMessage)
        return response

    result = await CodexProxyToolImageMiddleware().awrap_model_call(request, handler)

    assert result is response
    assert request.messages[-1] is tool
    assert tool.content == [_image("aW1hZ2U=", "image/png")]


def test_noop_for_direct_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_PROXY_BASE_URL", raising=False)
    model = ChatOpenAI(model="gpt-5.6-sol", api_key="test-key", use_responses_api=False)
    tool = ToolMessage(
        content=[_image("aW1hZ2U=", "image/png")],
        tool_call_id="call_1",
    )
    request = ModelRequest(
        model=model,
        messages=[AIMessage(content="", tool_calls=[_tool_call("call_1")]), tool],
    )

    def handler(outgoing: ModelRequest) -> MagicMock:
        assert outgoing is request
        return MagicMock()

    CodexProxyToolImageMiddleware().wrap_model_call(request, handler)


def test_noop_for_non_openai_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_PROXY_BASE_URL", "https://codex-proxy.example/v1")
    request = ModelRequest(
        model=MagicMock(),
        messages=[
            AIMessage(content="", tool_calls=[_tool_call("call_1")]),
            ToolMessage(
                content=[_image("aW1hZ2U=", "image/png")],
                tool_call_id="call_1",
            ),
        ],
    )

    def handler(outgoing: ModelRequest) -> MagicMock:
        assert outgoing is request
        return MagicMock()

    CodexProxyToolImageMiddleware().wrap_model_call(request, handler)


def test_noop_for_chat_openai_not_using_configured_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_PROXY_BASE_URL", "https://codex-proxy.example/v1")
    model = ChatOpenAI(
        model="gpt-5.6-sol",
        api_key="test-key",
        base_url="https://api.openai.example/v1",
        use_responses_api=False,
    )
    request = ModelRequest(
        model=model,
        messages=[
            AIMessage(content="", tool_calls=[_tool_call("call_1")]),
            ToolMessage(
                content=[_image("aW1hZ2U=", "image/png")],
                tool_call_id="call_1",
            ),
        ],
    )

    def handler(outgoing: ModelRequest) -> MagicMock:
        assert outgoing is request
        return MagicMock()

    CodexProxyToolImageMiddleware().wrap_model_call(request, handler)


def test_historical_images_are_stripped_after_text_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _codex_model(monkeypatch)
    old_image_tool = ToolMessage(
        content=[_image("b2xk", "image/png")],
        tool_call_id="old_call",
    )
    messages = [
        AIMessage(content="", tool_calls=[_tool_call("old_call")]),
        old_image_tool,
        AIMessage(
            content="Desktop hierarchy and spacing are visually sound.",
            tool_calls=[_tool_call("new_call", "execute")],
        ),
        ToolMessage(content="done", tool_call_id="new_call"),
    ]
    request = ModelRequest(model=model, messages=messages)
    captured: list[ModelRequest] = []

    def handler(outgoing: ModelRequest) -> MagicMock:
        captured.append(outgoing)
        return MagicMock()

    CodexProxyToolImageMiddleware().wrap_model_call(request, handler)

    assert captured[0] is not request
    assert captured[0].messages[-1].content == "done"
    assert not isinstance(captured[0].messages[-1], HumanMessage)
    stripped = captured[0].messages[1]
    assert isinstance(stripped, ToolMessage)
    assert all(block.get("type") != "image" for block in stripped.content)
    assert old_image_tool.content == [_image("b2xk", "image/png")]


def test_sequential_empty_tool_calls_keep_desktop_and_mobile_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _codex_model(monkeypatch)
    desktop = _image("ZGVza3RvcA==", "image/png")
    mobile = _image("bW9iaWxl", "image/png")
    messages = [
        AIMessage(content="", tool_calls=[_tool_call("desktop")]),
        ToolMessage(content=[desktop], tool_call_id="desktop"),
        AIMessage(content="", tool_calls=[_tool_call("mobile")]),
        ToolMessage(content=[mobile], tool_call_id="mobile"),
    ]
    request = ModelRequest(model=model, messages=messages)
    captured: list[ModelRequest] = []

    CodexProxyToolImageMiddleware().wrap_model_call(
        request,
        lambda outgoing: captured.append(outgoing) or MagicMock(),
    )

    image_message = captured[0].messages[-1]
    assert isinstance(image_message, HumanMessage)
    assert isinstance(image_message.content, list)
    assert [
        block["base64"]
        for block in image_message.content
        if isinstance(block, dict) and block.get("type") == "image"
    ] == ["ZGVza3RvcA==", "bW9iaWxl"]
    assert messages[1].content == [desktop]
    assert messages[3].content == [mobile]


def test_text_observation_releases_earlier_pending_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _codex_model(monkeypatch)
    messages = [
        AIMessage(content="", tool_calls=[_tool_call("desktop")]),
        ToolMessage(
            content=[_image("ZGVza3RvcA==", "image/png")],
            tool_call_id="desktop",
        ),
        AIMessage(
            content="Desktop: the title and KPI row are aligned and readable.",
            tool_calls=[_tool_call("mobile")],
        ),
        ToolMessage(
            content=[_image("bW9iaWxl", "image/png")],
            tool_call_id="mobile",
        ),
    ]
    request = ModelRequest(model=model, messages=messages)
    captured: list[ModelRequest] = []

    CodexProxyToolImageMiddleware().wrap_model_call(
        request,
        lambda outgoing: captured.append(outgoing) or MagicMock(),
    )

    image_message = captured[0].messages[-1]
    assert isinstance(image_message, HumanMessage)
    assert isinstance(image_message.content, list)
    assert [
        block["base64"]
        for block in image_message.content
        if isinstance(block, dict) and block.get("type") == "image"
    ] == ["bW9iaWxl"]


def test_pending_image_batch_keeps_only_eight_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _codex_model(monkeypatch)
    messages: list[AIMessage | ToolMessage] = []
    for index in range(10):
        call_id = f"call_{index}"
        messages.extend(
            [
                AIMessage(content="", tool_calls=[_tool_call(call_id)]),
                ToolMessage(
                    content=[_image(f"image_{index}", "image/png")],
                    tool_call_id=call_id,
                ),
            ]
        )
    request = ModelRequest(model=model, messages=messages)
    captured: list[ModelRequest] = []

    CodexProxyToolImageMiddleware().wrap_model_call(
        request,
        lambda outgoing: captured.append(outgoing) or MagicMock(),
    )

    image_message = captured[0].messages[-1]
    assert isinstance(image_message, HumanMessage)
    assert isinstance(image_message.content, list)
    assert [
        block["base64"]
        for block in image_message.content
        if isinstance(block, dict) and block.get("type") == "image"
    ] == [f"image_{index}" for index in range(2, 10)]


def test_pending_image_batch_respects_encoded_size_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "agent.middleware.codex_proxy_tool_images._MAX_PENDING_IMAGE_ENCODED_CHARS",
        10,
    )
    model = _codex_model(monkeypatch)
    messages: list[AIMessage | ToolMessage] = []
    for index, encoded in enumerate(("aaaaaa", "bbbbbb", "cccccc")):
        call_id = f"call_{index}"
        messages.extend(
            [
                AIMessage(content="", tool_calls=[_tool_call(call_id)]),
                ToolMessage(
                    content=[_image(encoded, "image/png")],
                    tool_call_id=call_id,
                ),
            ]
        )
    request = ModelRequest(model=model, messages=messages)
    captured: list[ModelRequest] = []

    CodexProxyToolImageMiddleware().wrap_model_call(
        request,
        lambda outgoing: captured.append(outgoing) or MagicMock(),
    )

    image_message = captured[0].messages[-1]
    assert isinstance(image_message, HumanMessage)
    assert isinstance(image_message.content, list)
    assert [
        block["base64"]
        for block in image_message.content
        if isinstance(block, dict) and block.get("type") == "image"
    ] == ["cccccc"]


def test_bridge_inserts_images_before_queued_human_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _codex_model(monkeypatch)
    tool = ToolMessage(
        content=[_image("aW1hZ2U=", "image/png")],
        tool_call_id="call_1",
    )
    queued = HumanMessage(content="Also check the mobile heading")
    original_messages = [
        AIMessage(content="", tool_calls=[_tool_call("call_1")]),
        tool,
        queued,
    ]
    request = ModelRequest(model=model, messages=original_messages)
    captured: list[ModelRequest] = []

    CodexProxyToolImageMiddleware().wrap_model_call(
        request,
        lambda outgoing: captured.append(outgoing) or MagicMock(),
    )

    outgoing = captured[0]
    assert isinstance(outgoing.messages[1], ToolMessage)
    assert isinstance(outgoing.messages[2], HumanMessage)
    assert isinstance(outgoing.messages[2].content, list)
    assert any(block.get("type") == "image" for block in outgoing.messages[2].content)
    assert outgoing.messages[3] is queued
    assert request.messages is original_messages
    assert request.messages[1] is tool
    assert request.messages[2] is queued


def test_general_purpose_subagent_installs_bridge() -> None:
    spec = _general_purpose_subagent(MagicMock())

    middleware = spec.get("middleware")
    assert isinstance(middleware, list)
    assert [type(item) for item in middleware] == [CodexProxyToolImageMiddleware]


def test_noop_for_incomplete_parallel_tool_group(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _codex_model(monkeypatch)
    request = ModelRequest(
        model=model,
        messages=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("call_1"), _tool_call("call_2")],
            ),
            ToolMessage(
                content=[_image("aW1hZ2U=", "image/png")],
                tool_call_id="call_1",
            ),
        ],
    )

    def handler(outgoing: ModelRequest) -> MagicMock:
        assert outgoing is request
        return MagicMock()

    CodexProxyToolImageMiddleware().wrap_model_call(request, handler)
