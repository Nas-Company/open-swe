from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from agent.middleware.notify_step_limit import notify_step_limit_reached
from agent.tools import lark_read_thread_messages, lark_thread_reply
from agent.utils import auth
from agent.utils.lark import LarkMessage

reply_module = importlib.import_module("agent.tools.lark_thread_reply")
read_module = importlib.import_module("agent.tools.lark_read_thread_messages")


@pytest.mark.asyncio
async def test_reply_uses_configured_root(monkeypatch: pytest.MonkeyPatch) -> None:
    post = AsyncMock(return_value=MagicMock(ok=True, message_id="om-reply"))
    monkeypatch.setattr(
        reply_module,
        "get_config",
        lambda: {"configurable": {"lark_thread": {"root_message_id": "om-root"}}},
    )
    monkeypatch.setattr(reply_module, "reply_to_lark_message", post)

    result = await lark_thread_reply("Done")

    assert result == {"success": True, "message_id": "om-reply"}
    post.assert_awaited_once_with("om-root", {"text": "Done"}, msg_type="text")


@pytest.mark.asyncio
async def test_reply_rejects_missing_thread_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reply_module,
        "get_config",
        lambda: {"configurable": {}},
    )

    result = await lark_thread_reply("Done")

    assert result["success"] is False
    assert "root_message_id" in result["error"]


@pytest.mark.asyncio
async def test_plan_approval_stores_random_pending_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    threads = SimpleNamespace(
        get=AsyncMock(return_value={"metadata": {}}),
        update=AsyncMock(side_effect=lambda **kwargs: updates.append(kwargs["metadata"])),
    )
    post = AsyncMock(return_value=MagicMock(ok=True, message_id="om-card"))
    monkeypatch.setattr(
        reply_module,
        "get_config",
        lambda: {
            "configurable": {
                "thread_id": "thread-1",
                "lark_thread": {"root_message_id": "om-root"},
            }
        },
    )
    monkeypatch.setattr(
        reply_module, "get_client", lambda **_kwargs: SimpleNamespace(threads=threads)
    )
    monkeypatch.setattr(reply_module, "reply_to_lark_message", post)

    await lark_thread_reply("Implement this plan", plan_approval=True)

    approvals = updates[0]["lark_plan_approvals"]
    assert isinstance(approvals, dict)
    fingerprint, record = next(iter(approvals.items()))
    assert len(fingerprint) >= 32
    assert record["status"] == "pending"
    assert (
        post.await_args.args[1]["body"]["elements"][1]["actions"][0]["value"]["fingerprint"]
        == fingerprint
    )


@pytest.mark.asyncio
async def test_option_card_carries_thread_and_one_time_action_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = AsyncMock(return_value=MagicMock(ok=True, message_id="om-card"))
    monkeypatch.setattr(
        reply_module,
        "get_config",
        lambda: {
            "configurable": {
                "thread_id": "thread-1",
                "lark_thread": {"root_message_id": "om-root"},
            }
        },
    )
    monkeypatch.setattr(reply_module, "reply_to_lark_message", post)

    await lark_thread_reply("Choose", options=["One", "Two"])

    actions = post.await_args.args[1]["body"]["elements"][1]["actions"]
    assert {action["value"]["thread_id"] for action in actions} == {"thread-1"}
    assert len({action["value"]["action_id"] for action in actions}) == 1


@pytest.mark.asyncio
async def test_read_tool_returns_normalized_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    message = LarkMessage(
        message_id="om-1",
        root_message_id="om-root",
        parent_message_id="",
        chat_id="oc-chat",
        chat_type="group",
        message_type="text",
        text="Fix it",
        mentions=(),
        image_keys=(),
        sender_id="ou-alice",
    )
    monkeypatch.setattr(
        read_module,
        "get_config",
        lambda: {
            "configurable": {"lark_thread": {"chat_id": "oc-chat", "root_message_id": "om-root"}}
        },
    )
    monkeypatch.setattr(
        read_module,
        "fetch_lark_thread",
        AsyncMock(return_value=(message,)),
    )
    monkeypatch.setattr(
        read_module,
        "get_lark_user",
        AsyncMock(return_value={"name": "Alice"}),
    )

    result = await lark_read_thread_messages()

    assert result["messages"] == [
        {"author": "Alice", "text": "Fix it", "message_id": "om-1", "image_keys": []}
    ]


def test_lark_auth_failure_posts_generic_connect_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://app.example.com")
    reply = AsyncMock()
    monkeypatch.setattr(auth, "reply_to_lark_message", reply, raising=False)
    monkeypatch.setattr(
        auth,
        "get_config",
        lambda: {
            "configurable": {
                "lark_thread": {"root_message_id": "om-root"},
            }
        },
    )

    asyncio.run(auth.leave_failure_comment("lark", "Click https://auth.example/secret-token"))

    content = reply.await_args.args[1]["text"]
    assert "secret-token" not in content
    assert "https://app.example.com/my-settings" in content


@pytest.mark.asyncio
async def test_step_limit_posts_to_lark_root() -> None:
    state = {"messages": [AIMessage(content="Model call limits exceeded: run limit reached")]}

    with (
        patch(
            "agent.middleware.notify_step_limit.get_config",
            return_value={
                "configurable": {
                    "source": "lark",
                    "lark_thread": {"root_message_id": "om-root"},
                }
            },
        ),
        patch(
            "agent.middleware.notify_step_limit.reply_to_lark_message",
            new_callable=AsyncMock,
            create=True,
        ) as post,
    ):
        result = await notify_step_limit_reached.aafter_agent(state, MagicMock())

    assert result is None
    assert post.await_args.args[0] == "om-root"
    assert "maximum step limit" in post.await_args.args[1]["text"]
