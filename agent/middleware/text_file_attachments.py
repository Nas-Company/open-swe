"""Materialize dashboard text-file attachments before model calls."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping

from deepagents.backends.protocol import SandboxBackendProtocol
from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime

from ..utils.sandbox_errors import is_retryable_sandbox_connection_error
from ..utils.sandbox_state import get_sandbox_backend
from ..utils.text_file_attachments import (
    TEXT_FILE_ATTACHMENT_ROOT,
    TextFileAttachment,
    validate_attachment_encoded_size,
    validate_text_file_blocks,
)
from .tool_error_handler import recreate_sandbox_from_config

logger = logging.getLogger(__name__)

BackendResolver = Callable[
    [str],
    SandboxBackendProtocol | Awaitable[SandboxBackendProtocol],
]
BackendRecreator = Callable[
    [str, Mapping[str, object]],
    Awaitable[SandboxBackendProtocol],
]


def _replacement_block(attachment: TextFileAttachment) -> dict[str, str]:
    return {
        "type": "text",
        "text": (
            f'Attached UTF-8 text file "{attachment.file_name}" is available in the sandbox at '
            f"`{attachment.sandbox_path}`. Read it from that exact path when needed."
        ),
    }


def _message_content(message: object) -> object:
    if isinstance(message, Mapping):
        return message.get("content")
    return getattr(message, "content", None)


def _replace_message_content(message: object, content: list[object]) -> object:
    if isinstance(message, BaseMessage):
        return message.model_copy(update={"content": content})
    if isinstance(message, Mapping):
        return {**message, "content": content}
    raise TypeError(f"unsupported message type: {type(message).__name__}")


def _response_value(response: object, key: str) -> object:
    if isinstance(response, Mapping):
        return response.get(key)
    return getattr(response, key, None)


def _configurable_from_runtime(runtime: Runtime) -> Mapping[str, object]:
    runtime_config = getattr(runtime, "config", None)
    config = runtime_config if isinstance(runtime_config, Mapping) else None
    if config is None:
        config = get_config()
    configurable = config.get("configurable", {})
    if not isinstance(configurable, Mapping):
        return {}
    return configurable


def _thread_id_from_runtime(runtime: Runtime) -> str | None:
    configurable = _configurable_from_runtime(runtime)
    thread_id = configurable.get("thread_id")
    return thread_id if isinstance(thread_id, str) and thread_id else None


class TextFileAttachmentMiddleware(AgentMiddleware):
    """Upload file blocks while transforming only the provider-facing request."""

    def __init__(
        self,
        *,
        backend_resolver: BackendResolver | None = None,
        backend_recreator: BackendRecreator | None = None,
    ) -> None:
        self._backend_resolver = backend_resolver or get_sandbox_backend
        self._backend_recreator = backend_recreator or recreate_sandbox_from_config
        self._materialized_paths: set[tuple[str, str]] = set()

    async def _resolve_backend(self, thread_id: str) -> SandboxBackendProtocol:
        backend = self._backend_resolver(thread_id)
        if inspect.isawaitable(backend):
            backend = await backend
        return backend

    def _attachments_for_message(self, message: HumanMessage) -> list[TextFileAttachment]:
        content = _message_content(message)
        if not isinstance(content, list):
            return []
        attachments = validate_text_file_blocks(content)
        validate_attachment_encoded_size(content)
        return attachments

    async def _upload_missing(
        self,
        backend: SandboxBackendProtocol,
        attachments: list[TextFileAttachment],
    ) -> None:
        sandbox_id = backend.id
        uploads_by_path = {
            attachment.sandbox_path: attachment.data
            for attachment in attachments
            if (sandbox_id, attachment.sandbox_path) not in self._materialized_paths
        }
        uploads = list(uploads_by_path.items())
        if not uploads:
            return

        mkdir_result = await backend.aexecute(f"mkdir -p -- {TEXT_FILE_ATTACHMENT_ROOT}")
        if _response_value(mkdir_result, "exit_code") != 0:
            output = _response_value(mkdir_result, "output")
            raise RuntimeError(
                f"failed to create attachment directory: {output or 'unknown error'}"
            )

        upload_results = await backend.aupload_files(uploads)
        if len(upload_results) != len(uploads):
            raise RuntimeError("sandbox returned an incomplete file upload response")
        for (path, _), result in zip(uploads, upload_results, strict=True):
            error = _response_value(result, "error")
            if error:
                raise RuntimeError(f"failed to upload {path}: {error}")
        self._materialized_paths.update((sandbox_id, path) for path, _ in uploads)

    async def _materialize_with_recovery(
        self,
        thread_id: str,
        configurable: Mapping[str, object],
        attachments: list[TextFileAttachment],
    ) -> None:
        attempted_sandbox_id: str | None = None
        try:
            backend = await self._resolve_backend(thread_id)
            attempted_sandbox_id = backend.id
            await self._upload_missing(backend, attachments)
            return
        except Exception as exc:
            if not is_retryable_sandbox_connection_error(exc):
                raise
            logger.warning(
                "Sandbox failed while materializing attachments for thread %s; recovering",
                thread_id,
                exc_info=True,
            )

        current_backend: SandboxBackendProtocol | None = None
        try:
            current_backend = await self._resolve_backend(thread_id)
        except Exception as exc:
            if not is_retryable_sandbox_connection_error(exc):
                raise

        if (
            current_backend is not None
            and attempted_sandbox_id is not None
            and current_backend.id != attempted_sandbox_id
        ):
            recovered_backend = current_backend
        else:
            recovered_backend = await self._backend_recreator(thread_id, configurable)
        await self._upload_missing(recovered_backend, attachments)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        attachments_by_index: dict[int, list[TextFileAttachment]] = {}
        all_attachments: list[TextFileAttachment] = []
        for index, message in enumerate(request.messages):
            if not isinstance(message, HumanMessage):
                continue
            attachments = self._attachments_for_message(message)
            if attachments:
                attachments_by_index[index] = attachments
                all_attachments.extend(attachments)

        if not all_attachments:
            return await handler(request)

        thread_id = _thread_id_from_runtime(request.runtime)
        if not thread_id:
            raise RuntimeError("cannot materialize file attachments without a thread_id")
        await self._materialize_with_recovery(
            thread_id,
            _configurable_from_runtime(request.runtime),
            all_attachments,
        )

        provider_messages = list(request.messages)
        for index, attachments in attachments_by_index.items():
            message = request.messages[index]
            content = _message_content(message)
            replacement_content = list(content) if isinstance(content, list) else []
            attachment_iter = iter(attachments)
            for block_index, block in enumerate(replacement_content):
                if isinstance(block, Mapping) and block.get("type") == "file":
                    replacement_content[block_index] = _replacement_block(next(attachment_iter))
            provider_messages[index] = _replace_message_content(message, replacement_content)

        return await handler(request.override(messages=provider_messages))
