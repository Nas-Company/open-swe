"""Validation and deterministic paths for UTF-8 text-file attachments."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

TEXT_FILE_ATTACHMENT_ROOT = "/workspace/.open-swe/attachments"
MAX_TEXT_FILES = 5
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_TEXT_FILES_TOTAL_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_ENCODED_BYTES = 20 * 1024 * 1024

_SUPPORTED_MIME_TYPES: dict[str, frozenset[str]] = {
    ".md": frozenset({"text/markdown", "text/plain"}),
    ".html": frozenset({"text/html"}),
    ".json": frozenset({"application/json"}),
    ".csv": frozenset({"text/csv"}),
    ".txt": frozenset({"text/plain"}),
}
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")


class TextFileAttachmentError(ValueError):
    """Raised when a text-file content block is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class TextFileAttachment:
    """A validated UTF-8 file ready to upload into a sandbox."""

    file_name: str
    mime_type: str
    data: bytes
    sandbox_path: str


def attachment_encoded_bytes(content: object) -> int:
    """Return base64 character bytes for image/file blocks nested in content."""
    if isinstance(content, Mapping):
        total = 0
        if content.get("type") in {"file", "image"}:
            for key in ("base64", "data"):
                encoded = content.get(key)
                if isinstance(encoded, str):
                    total += len(encoded.encode("utf-8"))
        return total + sum(attachment_encoded_bytes(value) for value in content.values())
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        return sum(attachment_encoded_bytes(value) for value in content)
    return 0


def validate_attachment_encoded_size(content: object) -> None:
    """Keep encoded attachment payloads safely below LangSmith's ingress limit."""
    if attachment_encoded_bytes(content) > MAX_ATTACHMENT_ENCODED_BYTES:
        limit_mib = MAX_ATTACHMENT_ENCODED_BYTES // (1024 * 1024)
        raise TextFileAttachmentError(
            f"attachments exceed the {limit_mib} MiB encoded payload limit"
        )


def _attachment_path(file_name: str, data: bytes) -> str:
    suffix = PurePath(file_name).suffix.lower()
    stem = PurePath(file_name).stem
    safe_stem = _SAFE_STEM_RE.sub("-", stem).strip("._-")[:80] or "attachment"
    digest = hashlib.sha256(file_name.encode("utf-8") + b"\0" + data).hexdigest()[:16]
    return f"{TEXT_FILE_ATTACHMENT_ROOT}/{digest}-{safe_stem}{suffix}"


def _validate_file_name(file_name: object) -> tuple[str, str]:
    if not isinstance(file_name, str) or not file_name or file_name != file_name.strip():
        raise TextFileAttachmentError("file_name must be a non-empty safe filename")
    if len(file_name.encode("utf-8")) > 255:
        raise TextFileAttachmentError("file_name exceeds 255 bytes")
    if file_name in {".", ".."} or "/" in file_name or "\\" in file_name:
        raise TextFileAttachmentError("file_name must not contain a path")
    if any(ord(character) < 32 or ord(character) == 127 for character in file_name):
        raise TextFileAttachmentError("file_name contains control characters")
    suffix = PurePath(file_name).suffix.lower()
    if suffix not in _SUPPORTED_MIME_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_MIME_TYPES))
        raise TextFileAttachmentError(f"unsupported file extension; expected one of: {supported}")
    return file_name, suffix


def _decode_file_block(block: Mapping[str, Any]) -> TextFileAttachment:
    if "data" in block:
        raise TextFileAttachmentError("file attachments must use base64 only")
    file_name, suffix = _validate_file_name(block.get("file_name"))
    mime_value = block.get("mime_type")
    if not isinstance(mime_value, str) or not mime_value.strip():
        raise TextFileAttachmentError(f"missing mime_type for {file_name}")
    mime_type = mime_value.split(";", 1)[0].strip().lower()
    if mime_type not in _SUPPORTED_MIME_TYPES[suffix]:
        raise TextFileAttachmentError(f"mime_type {mime_type!r} does not match {suffix}")

    encoded = block.get("base64")
    if not isinstance(encoded, str) or not encoded:
        raise TextFileAttachmentError(f"missing base64 data for {file_name}")
    max_encoded_length = 4 * ((MAX_TEXT_FILE_BYTES + 2) // 3)
    if len(encoded) > max_encoded_length:
        raise TextFileAttachmentError(
            f"{file_name} exceeds the {MAX_TEXT_FILE_BYTES // (1024 * 1024)}MB file limit"
        )
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TextFileAttachmentError(f"invalid base64 data for {file_name}") from exc
    if len(data) > MAX_TEXT_FILE_BYTES:
        raise TextFileAttachmentError(
            f"{file_name} exceeds the {MAX_TEXT_FILE_BYTES // (1024 * 1024)}MB file limit"
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TextFileAttachmentError(f"{file_name} must contain valid UTF-8 text") from exc

    return TextFileAttachment(
        file_name=file_name,
        mime_type=mime_type,
        data=data,
        sandbox_path=_attachment_path(file_name, data),
    )


def canonical_text_file_block(attachment: TextFileAttachment) -> dict[str, str]:
    """Return the single durable representation accepted by the agent runtime."""
    return {
        "type": "file",
        "base64": base64.b64encode(attachment.data).decode("ascii"),
        "mime_type": attachment.mime_type,
        "file_name": attachment.file_name,
    }


def validate_text_file_blocks(content: object) -> list[TextFileAttachment]:
    """Validate and decode every ``type=file`` block in a content list."""
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return []
    blocks = [
        block for block in content if isinstance(block, Mapping) and block.get("type") == "file"
    ]
    if len(blocks) > MAX_TEXT_FILES:
        raise TextFileAttachmentError(f"at most {MAX_TEXT_FILES} files are supported")
    attachments = [_decode_file_block(block) for block in blocks]
    total_bytes = sum(len(attachment.data) for attachment in attachments)
    if total_bytes > MAX_TEXT_FILES_TOTAL_BYTES:
        limit_mb = MAX_TEXT_FILES_TOTAL_BYTES // (1024 * 1024)
        raise TextFileAttachmentError(f"files exceed the {limit_mb}MB combined limit")
    return attachments
