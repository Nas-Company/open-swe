"""Publish a final sandbox file as a durable thread artifact."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import posixpath
import shlex
from pathlib import PurePosixPath
from typing import Any

from langgraph.config import get_config

from ..dashboard.artifacts import ARTIFACT_MAX_BYTES, artifact_to_api, publish_thread_artifact
from ..utils.dashboard_links import dashboard_artifact_download_url
from ..utils.sandbox_paths import aresolve_sandbox_work_dir
from ..utils.sandbox_state import get_sandbox_backend

logger = logging.getLogger(__name__)

_TRANSFER_CHUNK_BYTES = 512 * 1024

_BOUNDED_FILE_CHUNK_SCRIPT = r"""
import base64
import hashlib
import json
import os
import stat
import sys

(
    requested,
    workspace,
    raw_max_bytes,
    raw_chunk_bytes,
    raw_offset,
    expected_sha256,
    raw_expected_size,
) = sys.argv[1:8]
max_bytes = int(raw_max_bytes)
chunk_bytes = int(raw_chunk_bytes)
offset = int(raw_offset)
expected_size = int(raw_expected_size)
candidate = requested if os.path.isabs(requested) else os.path.join(workspace, requested)
resolved_workspace = os.path.realpath(workspace)
resolved = os.path.realpath(candidate)
is_symlink = False
directory_fds = []
try:
    info = os.lstat(candidate)
    is_symlink = stat.S_ISLNK(info.st_mode)
    common = os.path.commonpath([resolved_workspace, resolved])
    if common != resolved_workspace or is_symlink:
        raise ValueError("unsafe_path")

    relative = os.path.relpath(resolved, resolved_workspace)
    parts = relative.split(os.sep)
    if relative in {"", "."} or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("unsafe_path")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(resolved_workspace, directory_flags)
    directory_fds.append(parent_fd)
    for part in parts[:-1]:
        parent_fd = os.open(part, directory_flags, dir_fd=parent_fd)
        directory_fds.append(parent_fd)

    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(parts[-1], source_flags, dir_fd=parent_fd)
    with os.fdopen(source_fd, "rb") as source:
        target = os.fstat(source.fileno())
        if not stat.S_ISREG(target.st_mode):
            raise ValueError("not_regular")
        if target.st_size > max_bytes:
            raise ValueError("too_large")
        data = source.read(max_bytes + 1)

    if len(data) > max_bytes:
        raise ValueError("too_large")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ValueError("file_changed")
    if expected_size >= 0 and len(data) != expected_size:
        raise ValueError("file_changed")
    if offset < 0 or offset > len(data):
        raise ValueError("invalid_offset")

    payload = {
        "ok": True,
        "path": resolved,
        "size": len(data),
        "sha256": digest,
        "offset": offset,
        "data": base64.b64encode(data[offset : offset + chunk_bytes]).decode("ascii"),
        "is_symlink": False,
    }
except (FileNotFoundError, NotADirectoryError, PermissionError, OSError, ValueError) as exc:
    payload = {"ok": False, "error": str(exc), "is_symlink": is_symlink}
finally:
    for descriptor in reversed(directory_fds):
        try:
            os.close(descriptor)
        except OSError:
            pass
print(json.dumps(payload))
""".strip()

_BLOCKED_NAMES = frozenset(
    {
        ".env",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
    }
)
_BLOCKED_PATH_PARTS = frozenset(
    {
        ".aws",
        ".config",
        ".git",
        ".gnupg",
        ".kube",
        ".ssh",
    }
)
_BLOCKED_FILENAME_STEMS = frozenset(
    {
        "application_default_credentials",
        "credential",
        "credentials",
        "private-key",
        "private_key",
        "secret",
        "secrets",
        "service-account",
        "service_account",
        "token",
        "tokens",
    }
)


async def publish_artifact(file_path: str, file_name: str | None = None) -> dict[str, Any]:
    """Publish one final deliverable so it remains downloadable after the sandbox is gone.

    Call this only after the file has passed its final validation and, for rendered UI,
    visual QA. Publish user-facing deliverables such as HTML reports, PDFs, or archives;
    do not publish temporary screenshots, logs, credentials, inputs, or intermediate files.

    Args:
        file_path: Absolute path, or a path relative to the sandbox workspace.
        file_name: Optional user-facing download name. It must be a filename, not a path.

    Returns:
        A success object containing durable artifact metadata and an authenticated download URL.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        return {"success": False, "error": "file_path cannot be empty"}

    try:
        config = get_config()
    except Exception:
        config = {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        return {"success": False, "error": "no thread_id in run config"}

    try:
        manifest = await _publish_from_sandbox(
            thread_id, file_path=file_path.strip(), file_name=file_name
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("publish_artifact failed for thread %s", thread_id)
        return {"success": False, "error": f"failed to publish artifact: {exc}"}

    return {
        "success": True,
        "artifact": artifact_to_api(manifest),
        "downloadUrl": dashboard_artifact_download_url(thread_id, manifest["id"]),
    }


async def _publish_from_sandbox(
    thread_id: str, *, file_path: str, file_name: str | None
) -> dict[str, Any]:
    if file_name is not None and posixpath.basename(file_name.replace("\\", "/")) != file_name:
        raise ValueError("file_name must not contain a path")

    backend = await get_sandbox_backend(thread_id)
    work_dir = await aresolve_sandbox_work_dir(backend)
    _validate_artifact_source_path(file_path, work_dir=work_dir)
    content, resolved_path = await _read_bounded_sandbox_file(
        backend,
        file_path=file_path,
        work_dir=work_dir,
    )
    _validate_artifact_source_path(resolved_path, work_dir=work_dir)

    return await publish_thread_artifact(
        thread_id,
        source_path=resolved_path,
        content=content,
        display_name=file_name,
    )


def _validate_artifact_source_path(file_path: str, *, work_dir: str) -> None:
    candidate = file_path if posixpath.isabs(file_path) else posixpath.join(work_dir, file_path)
    normalized_work_dir = posixpath.normpath(work_dir)
    normalized_path = posixpath.normpath(candidate)
    try:
        common_path = posixpath.commonpath([normalized_work_dir, normalized_path])
    except ValueError as exc:
        raise ValueError("file must be a regular file inside the sandbox workspace") from exc
    if common_path != normalized_work_dir:
        raise ValueError("file must be a regular file inside the sandbox workspace")

    blocked_name = posixpath.basename(normalized_path).lower()
    relative_parts = PurePosixPath(posixpath.relpath(normalized_path, normalized_work_dir)).parts
    blocked_stems: set[str] = set()
    remaining_name = blocked_name
    while "." in remaining_name:
        remaining_name = remaining_name.rsplit(".", 1)[0]
        blocked_stems.add(remaining_name)
    blocked_stems.add(remaining_name)
    is_attachment = relative_parts[:2] == (".open-swe", "attachments")
    is_outbox = relative_parts[:2] == (".open-swe", "deliverables")
    artifact_parts = relative_parts[2:] if is_outbox else relative_parts
    if (
        blocked_name in _BLOCKED_NAMES
        or blocked_name.startswith(".env")
        or is_attachment
        or any(part.startswith(".") for part in artifact_parts)
        or any(part.casefold() in _BLOCKED_PATH_PARTS for part in relative_parts)
        or bool(blocked_stems & _BLOCKED_FILENAME_STEMS)
    ):
        raise ValueError("credential and hidden configuration files cannot be published")


async def _read_bounded_sandbox_file(
    backend: Any,
    *,
    file_path: str,
    work_dir: str,
) -> tuple[bytes, str]:
    content = bytearray()
    expected_path: str | None = None
    expected_size: int | None = None
    expected_sha256: str | None = None

    while expected_size is None or len(content) < expected_size:
        command = " ".join(
            [
                "python -c",
                shlex.quote(_BOUNDED_FILE_CHUNK_SCRIPT),
                shlex.quote(file_path),
                shlex.quote(work_dir),
                str(ARTIFACT_MAX_BYTES),
                str(_TRANSFER_CHUNK_BYTES),
                str(len(content)),
                shlex.quote(expected_sha256 or ""),
                str(expected_size if expected_size is not None else -1),
            ]
        )
        result = await backend.aexecute(command, timeout=30)
        if result.exit_code != 0 or getattr(result, "truncated", False):
            raise ValueError("could not read the requested file safely")
        try:
            payload = json.loads(result.output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("sandbox returned an invalid file read result") from exc
        if not isinstance(payload, dict):
            raise ValueError("sandbox returned an invalid file read result")
        if not payload.get("ok"):
            if payload.get("is_symlink"):
                raise ValueError("symbolic links cannot be published")
            error = payload.get("error")
            if error == "too_large":
                raise ValueError(f"file exceeds the {ARTIFACT_MAX_BYTES}-byte artifact limit")
            if error == "file_changed":
                raise ValueError(
                    "file changed while it was being published; validate it and try again"
                )
            raise ValueError("file must be a regular file inside the sandbox workspace")

        resolved_path = payload.get("path")
        size = payload.get("size")
        digest = payload.get("sha256")
        offset = payload.get("offset")
        encoded = payload.get("data")
        if (
            not isinstance(resolved_path, str)
            or not isinstance(size, int)
            or not isinstance(digest, str)
            or not isinstance(offset, int)
            or not isinstance(encoded, str)
        ):
            raise ValueError("sandbox returned incomplete file metadata")
        if (
            not resolved_path.startswith("/")
            or len(resolved_path) > 4096
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("sandbox returned invalid file metadata")
        if size < 0 or size > ARTIFACT_MAX_BYTES:
            raise ValueError(f"file exceeds the {ARTIFACT_MAX_BYTES}-byte artifact limit")
        if offset != len(content):
            raise ValueError("sandbox returned an invalid file chunk offset")
        max_encoded_length = 4 * ((_TRANSFER_CHUNK_BYTES + 2) // 3)
        if len(encoded) > max_encoded_length:
            raise ValueError("sandbox returned an oversized file chunk")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("sandbox returned invalid file data") from exc
        if len(chunk) > _TRANSFER_CHUNK_BYTES:
            raise ValueError("sandbox returned an oversized file chunk")

        if expected_size is None:
            expected_path = resolved_path
            expected_size = size
            expected_sha256 = digest
        elif resolved_path != expected_path or size != expected_size or digest != expected_sha256:
            raise ValueError("file changed while it was being published; validate it and try again")

        remaining = expected_size - len(content)
        if len(chunk) != min(_TRANSFER_CHUNK_BYTES, remaining):
            raise ValueError("sandbox returned an incomplete file chunk")
        content.extend(chunk)

    if expected_path is None or expected_size is None or expected_sha256 is None:
        raise ValueError("sandbox returned incomplete file metadata")
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("sandbox file checksum validation failed")
    return bytes(content), expected_path
