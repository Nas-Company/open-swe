from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.utils.lark import LarkEvent, LarkMessage, LarkSender
from agent.webhooks import lark as lark_webhook


def _message(
    text: str,
    *,
    message_id: str = "om-message",
    image_keys: tuple[str, ...] = (),
    mentions: tuple[str, ...] = ("ou-bot",),
    sender_type: str = "user",
) -> LarkMessage:
    return LarkMessage(
        message_id=message_id,
        root_message_id="om-root",
        parent_message_id="om-root",
        chat_id="oc-chat",
        chat_type="group",
        message_type="text",
        text=text,
        mentions=mentions,
        image_keys=image_keys,
        sender_type=sender_type,
    )


def _event(*, text: str, image_keys: tuple[str, ...] = (), event_id: str = "evt-1") -> LarkEvent:
    return LarkEvent(
        event_id=event_id,
        tenant_key="tenant-a",
        sender=LarkSender(
            open_id="ou-alice",
            union_id="on-alice",
            user_id="alice",
            sender_type="user",
            tenant_key="tenant-a",
        ),
        message=_message(text, image_keys=image_keys),
    )


def test_no_repo_never_uses_default() -> None:
    result = lark_webhook.extract_lark_repo_refs([_message("please fix it")])

    assert result.status == "missing"
    assert result.repo is None


def test_pr_url_selects_parent_repo() -> None:
    result = lark_webhook.extract_lark_repo_refs(
        [_message("https://github.com/Nas-Company/nas-e2e/pull/44")]
    )

    assert result.status == "selected"
    assert result.repo == {"owner": "Nas-Company", "name": "nas-e2e"}


def test_multiple_repositories_require_disambiguation() -> None:
    result = lark_webhook.extract_lark_repo_refs(
        [
            _message(
                "compare https://github.com/Nas-Company/one and "
                "https://github.com/Nas-Company/two/pull/2"
            )
        ]
    )

    assert result.status == "ambiguous"
    assert result.repositories == (
        {"owner": "Nas-Company", "name": "one"},
        {"owner": "Nas-Company", "name": "two"},
    )


def test_context_starts_at_previous_mention_and_ignores_bot_messages() -> None:
    messages = [
        _message(
            "old https://github.com/Nas-Company/old",
            message_id="om-root",
            mentions=(),
        ),
        _message("working", message_id="om-bot", mentions=(), sender_type="app"),
        _message(
            "@openswe use https://github.com/Nas-Company/nas-e2e",
            message_id="om-previous",
        ),
        _message("current follow-up", message_id="om-current", mentions=()),
    ]

    context = lark_webhook.select_lark_context(messages, "om-current")

    assert [message.message_id for message in context] == ["om-previous", "om-current"]


def test_context_does_not_treat_plain_mention_text_as_structured_mention() -> None:
    messages = [
        _message("original context", message_id="om-root", mentions=()),
        _message("someone typed @openswe literally", message_id="om-text", mentions=()),
        _message("current", message_id="om-current", mentions=()),
    ]

    context = lark_webhook.select_lark_context(messages, "om-current")

    assert [message.message_id for message in context] == ["om-root", "om-text", "om-current"]


def _configure_happy_path(monkeypatch: pytest.MonkeyPatch, event: LarkEvent) -> dict[str, object]:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        lark_webhook,
        "fetch_lark_thread",
        AsyncMock(return_value=(event.message,)),
    )
    monkeypatch.setattr(
        lark_webhook,
        "get_lark_user",
        AsyncMock(return_value={"name": "Alice", "email": "alice@nas.io"}),
    )
    monkeypatch.setattr(lark_webhook, "login_for_lark_id", AsyncMock(return_value="alice"))
    monkeypatch.setattr(lark_webhook, "login_for_email", AsyncMock(return_value=None))
    monkeypatch.setattr(
        lark_webhook,
        "is_user_active_org_member",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(lark_webhook, "_is_repo_allowed", lambda _repo: True)
    monkeypatch.setattr(
        lark_webhook,
        "is_review_repo_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        lark_webhook,
        "get_github_app_installation_token",
        AsyncMock(return_value="app-token"),
    )
    monkeypatch.setattr(
        lark_webhook,
        "resolve_agent_model_id",
        AsyncMock(return_value="anthropic:claude-sonnet-4-5"),
    )
    monkeypatch.setattr(lark_webhook, "model_supports_images", lambda _model: True)
    monkeypatch.setattr(lark_webhook, "get_thread_active_status", AsyncMock(return_value=False))
    monkeypatch.setattr(lark_webhook, "queue_message_for_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(
        lark_webhook,
        "upsert_agent_thread_owner_metadata",
        AsyncMock(),
    )
    monkeypatch.setattr(lark_webhook, "get_client", lambda **_kwargs: SimpleNamespace())

    async def dispatch(
        thread_id: str,
        content: object,
        configurable: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, str]:
        calls["thread_id"] = thread_id
        calls["content"] = content
        calls["configurable"] = configurable
        return {"run_id": "run-1"}

    monkeypatch.setattr(lark_webhook, "dispatch_agent_run", dispatch)
    monkeypatch.setattr(lark_webhook, "get_lark_event_record", AsyncMock(return_value=None))
    monkeypatch.setattr(lark_webhook, "reply_to_lark_message", AsyncMock())
    monkeypatch.setattr(lark_webhook, "mark_lark_event_dispatched", AsyncMock())
    return calls


@pytest.mark.asyncio
async def test_retry_reconciles_existing_event_run_without_dispatching_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(text="review https://github.com/Nas-Company/nas-e2e")
    calls = _configure_happy_path(monkeypatch, event)
    monkeypatch.setattr(
        lark_webhook,
        "get_lark_event_record",
        AsyncMock(return_value=SimpleNamespace(attempts=2)),
    )
    runs = SimpleNamespace(
        list=AsyncMock(
            return_value=[{"run_id": "run-existing", "metadata": {"lark_event_id": event.event_id}}]
        )
    )
    monkeypatch.setattr(lark_webhook, "get_client", lambda **_kwargs: SimpleNamespace(runs=runs))
    dispatch = AsyncMock()
    monkeypatch.setattr(lark_webhook, "dispatch_agent_run", dispatch)

    await lark_webhook.process_lark_mention(event)

    dispatch.assert_not_awaited()
    lark_webhook.mark_lark_event_dispatched.assert_awaited_once_with(event.event_id, "run-existing")
    assert "thread_id" not in calls


@pytest.mark.asyncio
async def test_mapped_member_dispatches_one_lark_run_with_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(
        text="review https://github.com/Nas-Company/nas-e2e/pull/44",
        image_keys=("img-1",),
    )
    calls = _configure_happy_path(monkeypatch, event)
    monkeypatch.setattr(
        lark_webhook,
        "download_lark_image",
        AsyncMock(return_value=b"\x89PNG\r\n\x1a\nimage"),
    )

    await lark_webhook.process_lark_mention(event)

    configurable = calls["configurable"]
    assert isinstance(configurable, dict)
    assert configurable["source"] == "lark"
    assert configurable["github_login"] == "alice"
    assert configurable["repo"] == {"owner": "Nas-Company", "name": "nas-e2e"}
    content = calls["content"]
    assert isinstance(content, list)
    assert any(block.get("type") == "image" for block in content)


@pytest.mark.asyncio
async def test_unmapped_member_gets_connect_link_and_no_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(text="review https://github.com/Nas-Company/nas-e2e")
    dispatch = AsyncMock()
    reply = AsyncMock()
    monkeypatch.setattr(lark_webhook, "login_for_lark_id", AsyncMock(return_value=None))
    monkeypatch.setattr(lark_webhook, "login_for_email", AsyncMock(return_value=None))
    monkeypatch.setattr(
        lark_webhook,
        "get_lark_user",
        AsyncMock(return_value={"email": "different@nas.io"}),
    )
    monkeypatch.setattr(lark_webhook, "build_settings_url", lambda: "https://open-swe/settings")
    monkeypatch.setattr(lark_webhook, "reply_to_lark_message", reply)
    monkeypatch.setattr(lark_webhook, "dispatch_agent_run", dispatch)
    monkeypatch.setattr(lark_webhook, "mark_lark_event_dispatched", AsyncMock())

    await lark_webhook.process_lark_mention(event)

    assert "Connect Lark account" in reply.await_args.args[1]["text"]
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_repo_asks_user_to_choose_one(monkeypatch: pytest.MonkeyPatch) -> None:
    event = _event(text="please review this")
    _configure_happy_path(monkeypatch, event)
    reply = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(lark_webhook, "reply_to_lark_message", reply)
    monkeypatch.setattr(lark_webhook, "dispatch_agent_run", dispatch)

    await lark_webhook.process_lark_mention(event)

    assert "GitHub repository or PR link" in reply.await_args.args[1]["text"]
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversized_image_is_skipped_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    event = _event(
        text="review https://github.com/Nas-Company/nas-e2e",
        image_keys=("img-1",),
    )
    calls = _configure_happy_path(monkeypatch, event)
    monkeypatch.setattr(
        lark_webhook,
        "download_lark_image",
        AsyncMock(return_value=b"x" * (lark_webhook.LARK_IMAGE_MAX_BYTES + 1)),
    )

    await lark_webhook.process_lark_mention(event)

    content = calls["content"]
    assert isinstance(content, list)
    text = json.dumps(content)
    assert "too large" in text
    assert '"type": "image"' not in text


@pytest.mark.asyncio
async def test_failed_image_download_preserves_text_and_remaining_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(
        text="review https://github.com/Nas-Company/nas-e2e",
        image_keys=("expired", "valid"),
    )
    calls = _configure_happy_path(monkeypatch, event)

    async def download(_message_id: str, image_key: str) -> bytes:
        if image_key == "expired":
            raise RuntimeError("expired")
        return b"\x89PNG\r\n\x1a\nimage"

    monkeypatch.setattr(lark_webhook, "download_lark_image", download)

    await lark_webhook.process_lark_mention(event)

    content = calls["content"]
    assert isinstance(content, list)
    assert any(block.get("type") == "image" for block in content)
    assert "could not be downloaded" in json.dumps(content)


@pytest.mark.asyncio
async def test_followup_queues_into_active_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    event = _event(
        text="review https://github.com/Nas-Company/nas-e2e",
        event_id="evt-followup",
    )
    _configure_happy_path(monkeypatch, event)
    queue = AsyncMock(return_value=True)
    dispatch = AsyncMock()
    monkeypatch.setattr(lark_webhook, "get_thread_active_status", AsyncMock(return_value=True))
    monkeypatch.setattr(lark_webhook, "queue_message_for_thread", queue)
    monkeypatch.setattr(lark_webhook, "dispatch_agent_run", dispatch)

    await lark_webhook.process_lark_mention(event)

    queued = queue.await_args.args[1]
    assert queued["source"] == "lark"
    dispatch.assert_not_awaited()
