from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langsmith.sandbox import SandboxClientError

from agent.middleware.text_file_attachments import (
    TextFileAttachmentMiddleware,
)
from agent.utils import text_file_attachments
from agent.utils.text_file_attachments import (
    TEXT_FILE_ATTACHMENT_ROOT,
    TextFileAttachmentError,
    validate_attachment_encoded_size,
    validate_text_file_blocks,
)


def _file_block(
    file_name: str = "notes.md",
    data: bytes = b"hello",
    mime_type: str | None = None,
) -> dict[str, str]:
    if mime_type is None:
        mime_type = {
            ".md": "text/markdown",
            ".html": "text/html",
            ".json": "application/json",
            ".csv": "text/csv",
            ".txt": "text/plain",
        }.get(file_name[file_name.rfind(".") :].lower(), "application/octet-stream")
    return {
        "type": "file",
        "base64": base64.b64encode(data).decode("ascii"),
        "mime_type": mime_type,
        "file_name": file_name,
    }


@pytest.mark.parametrize(
    ("file_name", "mime_type"),
    [
        ("report.md", "text/markdown"),
        ("report.md", "text/plain"),
        ("report.html", "text/html"),
        ("data.json", "application/json"),
        ("data.csv", "text/csv"),
        ("notes.txt", "text/plain"),
    ],
)
def test_validate_text_file_blocks_accepts_supported_utf8_files(
    file_name: str, mime_type: str
) -> None:
    attachment = validate_text_file_blocks([_file_block(file_name, "你好".encode(), mime_type)])[0]

    assert attachment.file_name == file_name
    assert attachment.mime_type == mime_type
    assert attachment.data == "你好".encode()
    assert attachment.sandbox_path.startswith(f"{TEXT_FILE_ATTACHMENT_ROOT}/")
    assert attachment.sandbox_path.endswith(file_name[file_name.rfind(".") :])


@pytest.mark.parametrize(
    ("block", "error"),
    [
        ({**_file_block(), "base64": "not base64!"}, "invalid base64"),
        ({**_file_block(), "data": "aGVsbG8="}, "must use base64 only"),
        (_file_block(data=b"\xff"), "valid UTF-8"),
        (_file_block("../secret.md"), "must not contain a path"),
        (_file_block("secret.exe", mime_type="text/plain"), "unsupported file extension"),
        (_file_block("report.html", mime_type="text/plain"), "does not match"),
    ],
)
def test_validate_text_file_blocks_rejects_invalid_files(block: dict[str, str], error: str) -> None:
    with pytest.raises(TextFileAttachmentError, match=error):
        validate_text_file_blocks([block])


def test_validate_text_file_blocks_enforces_count_file_and_total_limits(monkeypatch) -> None:
    monkeypatch.setattr(text_file_attachments, "MAX_TEXT_FILES", 1)
    with pytest.raises(TextFileAttachmentError, match="at most 1"):
        validate_text_file_blocks([_file_block("a.txt"), _file_block("b.txt")])

    monkeypatch.setattr(text_file_attachments, "MAX_TEXT_FILES", 5)
    monkeypatch.setattr(text_file_attachments, "MAX_TEXT_FILE_BYTES", 3)
    with pytest.raises(TextFileAttachmentError, match="file limit"):
        validate_text_file_blocks([_file_block("a.txt", b"four")])

    monkeypatch.setattr(text_file_attachments, "MAX_TEXT_FILE_BYTES", 10)
    monkeypatch.setattr(text_file_attachments, "MAX_TEXT_FILES_TOTAL_BYTES", 5)
    with pytest.raises(TextFileAttachmentError, match="combined limit"):
        validate_text_file_blocks([_file_block("a.txt", b"aaa"), _file_block("b.txt", b"bbb")])


def test_validate_attachment_encoded_size_counts_images_and_files(monkeypatch) -> None:
    monkeypatch.setattr(text_file_attachments, "MAX_ATTACHMENT_ENCODED_BYTES", 15)

    with pytest.raises(TextFileAttachmentError, match="encoded payload limit"):
        validate_attachment_encoded_size(
            [
                {"type": "image", "base64": "a" * 8},
                {"type": "file", "base64": "b" * 8},
            ]
        )


def test_validate_attachment_encoded_size_counts_every_payload_carrier(monkeypatch) -> None:
    monkeypatch.setattr(text_file_attachments, "MAX_ATTACHMENT_ENCODED_BYTES", 10)

    with pytest.raises(TextFileAttachmentError, match="encoded payload limit"):
        validate_attachment_encoded_size(
            [
                {
                    "type": "file",
                    "base64": "a" * 4,
                    "data": "b" * 8,
                }
            ]
        )


def test_text_file_sandbox_path_is_deterministic_and_safe() -> None:
    first = validate_text_file_blocks([_file_block("Quarterly report 你好.MD")])[0]
    second = validate_text_file_blocks([_file_block("Quarterly report 你好.MD")])[0]

    assert first.sandbox_path == second.sandbox_path
    assert first.sandbox_path.endswith("-Quarterly-report.md")
    assert " " not in first.sandbox_path
    assert "你好" not in first.sandbox_path


class _FakeBackend:
    def __init__(self, *, backend_id: str = "sandbox-1", upload_error: str | None = None) -> None:
        self.id = backend_id
        self.commands: list[str] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.upload_error = upload_error

    async def aexecute(self, command: str) -> dict[str, Any]:
        self.commands.append(command)
        return {"output": "", "exit_code": 0, "truncated": False}

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
        self.uploads.extend(files)
        return [{"path": path, "error": self.upload_error} for path, _ in files]


def _model_request(messages: list[HumanMessage], *, thread_id: str | None) -> ModelRequest:
    configurable = {"thread_id": thread_id} if thread_id else {}
    return ModelRequest(
        model=MagicMock(),
        messages=messages,
        runtime=SimpleNamespace(config={"configurable": configurable}),
    )


@pytest.mark.asyncio
async def test_middleware_uploads_files_and_replaces_provider_facing_blocks() -> None:
    backend = _FakeBackend(backend_id="sandbox-1")
    active_backend = [backend]
    resolved_thread_ids: list[str] = []

    def resolve_backend(thread_id: str) -> _FakeBackend:
        resolved_thread_ids.append(thread_id)
        return active_backend[0]

    middleware = TextFileAttachmentMiddleware(backend_resolver=resolve_backend)
    message = HumanMessage(
        id="message-1",
        content=[
            {"type": "text", "text": "Use this report"},
            _file_block("Quarterly report.md", b"# Report"),
            {"type": "image", "base64": "aW1hZ2U=", "mime_type": "image/png"},
        ],
    )
    request = _model_request([message], thread_id="thread-1")
    provider_requests: list[ModelRequest] = []

    async def handler(provider_request: ModelRequest) -> ModelResponse:
        provider_requests.append(provider_request)
        return ModelResponse(result=[AIMessage(content="ok")])

    await middleware.awrap_model_call(request, handler)

    assert resolved_thread_ids == ["thread-1"]
    assert backend.commands == [f"mkdir -p -- {TEXT_FILE_ATTACHMENT_ROOT}"]
    assert len(backend.uploads) == 1
    path, data = backend.uploads[0]
    assert path.startswith(f"{TEXT_FILE_ATTACHMENT_ROOT}/")
    assert data == b"# Report"

    updated = provider_requests[0].messages[0]
    assert updated.id == "message-1"
    assert updated.content[0] == {"type": "text", "text": "Use this report"}
    assert updated.content[2] == {
        "type": "image",
        "base64": "aW1hZ2U=",
        "mime_type": "image/png",
    }
    replacement = updated.content[1]
    assert replacement["type"] == "text"
    assert path in replacement["text"]
    assert "base64" not in replacement
    assert message.content[1] == _file_block("Quarterly report.md", b"# Report")

    await middleware.awrap_model_call(request, handler)
    assert len(backend.uploads) == 1
    assert provider_requests[1].messages[0].content[1]["type"] == "text"

    recreated_backend = _FakeBackend(backend_id="sandbox-2")
    active_backend[0] = recreated_backend
    await middleware.awrap_model_call(request, handler)

    assert recreated_backend.commands == [f"mkdir -p -- {TEXT_FILE_ATTACHMENT_ROOT}"]
    assert recreated_backend.uploads == backend.uploads
    assert provider_requests[2].messages[0].content[1]["type"] == "text"
    assert message.content[1]["type"] == "file"


@pytest.mark.asyncio
async def test_middleware_fails_closed_without_thread_or_after_upload_error() -> None:
    message = HumanMessage(id="message-1", content=[_file_block("notes.txt")])
    middleware = TextFileAttachmentMiddleware(backend_resolver=lambda _: _FakeBackend())

    async def handler(_: ModelRequest) -> ModelResponse:
        return ModelResponse(result=[AIMessage(content="ok")])

    with pytest.raises(RuntimeError, match="without a thread_id"):
        await middleware.awrap_model_call(
            _model_request([message], thread_id=None),
            handler,
        )

    failing = TextFileAttachmentMiddleware(
        backend_resolver=lambda _: _FakeBackend(upload_error="permission_denied")
    )
    with pytest.raises(RuntimeError, match="permission_denied"):
        await failing.awrap_model_call(
            _model_request([message], thread_id="thread-1"),
            handler,
        )


@pytest.mark.asyncio
async def test_middleware_recovers_retryable_materialization_failure_once() -> None:
    class FailingBackend(_FakeBackend):
        async def aexecute(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            raise SandboxClientError("sandbox expired")

    failed_backend = FailingBackend(backend_id="sandbox-old")
    recovered_backend = _FakeBackend(backend_id="sandbox-new")
    recreations: list[tuple[str, dict[str, object]]] = []

    async def recreate(thread_id: str, configurable: dict[str, object]) -> _FakeBackend:
        recreations.append((thread_id, dict(configurable)))
        return recovered_backend

    middleware = TextFileAttachmentMiddleware(
        backend_resolver=lambda _: failed_backend,
        backend_recreator=recreate,
    )
    request = ModelRequest(
        model=MagicMock(),
        messages=[HumanMessage(content=[_file_block("notes.txt")])],
        runtime=SimpleNamespace(
            config={
                "configurable": {
                    "thread_id": "thread-1",
                    "repo": {"owner": "Nas-Company", "name": "nas-reporting"},
                }
            }
        ),
    )
    provider_requests: list[ModelRequest] = []

    async def handler(provider_request: ModelRequest) -> ModelResponse:
        provider_requests.append(provider_request)
        return ModelResponse(result=[AIMessage(content="ok")])

    await middleware.awrap_model_call(request, handler)

    assert recreations == [
        (
            "thread-1",
            {
                "thread_id": "thread-1",
                "repo": {"owner": "Nas-Company", "name": "nas-reporting"},
            },
        )
    ]
    assert len(failed_backend.commands) == 1
    assert len(recovered_backend.uploads) == 1
    assert provider_requests[0].messages[0].content[0]["type"] == "text"


@pytest.mark.asyncio
async def test_middleware_avoids_duplicate_concurrent_recreation() -> None:
    recovered_backend = _FakeBackend(backend_id="sandbox-new")
    active_backend: list[_FakeBackend] = []

    class SwappingBackend(_FakeBackend):
        async def aexecute(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            active_backend[0] = recovered_backend
            raise SandboxClientError("sandbox expired")

    failed_backend = SwappingBackend(backend_id="sandbox-old")
    active_backend.append(failed_backend)
    recreator = AsyncMock(side_effect=AssertionError("must not recreate twice"))
    middleware = TextFileAttachmentMiddleware(
        backend_resolver=lambda _: active_backend[0],
        backend_recreator=recreator,
    )

    async def handler(_: ModelRequest) -> ModelResponse:
        return ModelResponse(result=[AIMessage(content="ok")])

    await middleware.awrap_model_call(
        _model_request(
            [HumanMessage(content=[_file_block("notes.txt")])],
            thread_id="thread-1",
        ),
        handler,
    )

    recreator.assert_not_awaited()
    assert len(recovered_backend.uploads) == 1
