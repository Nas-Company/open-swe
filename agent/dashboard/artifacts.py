"""Durable storage for files published by an agent run."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from weakref import WeakValueDictionary

from langgraph_sdk import get_client

ARTIFACT_TTL_MINUTES = 43_200
ARTIFACT_MAX_BYTES = 20 * 1024 * 1024
ARTIFACT_MAX_COUNT_PER_THREAD = 10
ARTIFACT_MAX_TOTAL_BYTES_PER_THREAD = 50 * 1024 * 1024
ARTIFACT_CHUNK_BYTES = 1024 * 1024

_MANIFEST_NAMESPACE = ("thread_artifacts", "manifest")
_CHUNK_NAMESPACE = ("thread_artifacts", "chunks")
_ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PUBLISH_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class ArtifactError(RuntimeError):
    pass


class ArtifactNotFoundError(ArtifactError):
    pass


class ArtifactLimitError(ArtifactError):
    pass


class ArtifactCorruptError(ArtifactError):
    pass


def _client() -> Any:
    return get_client()


def _manifest_namespace(thread_id: str) -> list[str]:
    return [*_MANIFEST_NAMESPACE, thread_id]


def _chunk_namespace(thread_id: str, artifact_id: str) -> list[str]:
    return [*_CHUNK_NAMESPACE, thread_id, artifact_id]


def _publish_lock(thread_id: str) -> asyncio.Lock:
    lock = _PUBLISH_LOCKS.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        _PUBLISH_LOCKS[thread_id] = lock
    return lock


def _item_value(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
    return value if isinstance(value, dict) else None


def _search_values(result: Any) -> list[dict[str, Any]]:
    items = result.get("items", []) if isinstance(result, dict) else getattr(result, "items", [])
    return [value for item in items if (value := _item_value(item)) is not None]


def _safe_filename(source_path: str, display_name: str | None) -> str:
    candidate = (display_name or source_path).replace("\\", "/")
    candidate = PurePosixPath(candidate).name.strip()
    candidate = "".join(char for char in candidate if char >= " " and char != "\x7f")
    candidate = candidate[:255].strip(". ")
    return candidate or "artifact"


def artifact_to_api(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": manifest["id"],
        "fileName": manifest["filename"],
        "mimeType": manifest["mime_type"],
        "sizeBytes": manifest["size_bytes"],
        "sha256": manifest["sha256"],
        "createdAt": manifest["created_at"],
        "expiresAt": manifest["expires_at"],
    }


def artifact_content_disposition(filename: str) -> str:
    fallback = "".join(
        char if " " <= char <= "~" and char not in {'"', "\\"} else "_" for char in filename
    ).strip()
    fallback = fallback or "artifact"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def list_thread_artifacts(thread_id: str) -> list[dict[str, Any]]:
    result = await _client().store.search_items(
        _manifest_namespace(thread_id),
        limit=1000,
        refresh_ttl=False,
    )
    manifests = [item for item in _search_values(result) if _valid_manifest(item)]
    manifests.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return manifests


async def publish_thread_artifact(
    thread_id: str,
    *,
    source_path: str,
    content: bytes,
    display_name: str | None = None,
) -> dict[str, Any]:
    if len(content) > ARTIFACT_MAX_BYTES:
        raise ArtifactLimitError(
            f"file is {len(content)} bytes; maximum artifact size is {ARTIFACT_MAX_BYTES} bytes"
        )

    async with _publish_lock(thread_id):
        return await _publish_thread_artifact_locked(
            thread_id,
            source_path=source_path,
            content=content,
            display_name=display_name,
        )


async def _publish_thread_artifact_locked(
    thread_id: str,
    *,
    source_path: str,
    content: bytes,
    display_name: str | None,
) -> dict[str, Any]:
    filename = _safe_filename(source_path, display_name)
    digest = hashlib.sha256(content).hexdigest()
    existing = await list_thread_artifacts(thread_id)
    for manifest in existing:
        if (
            manifest.get("source_path") == source_path
            and manifest.get("filename") == filename
            and manifest.get("sha256") == digest
        ):
            return manifest

    if len(existing) >= ARTIFACT_MAX_COUNT_PER_THREAD:
        raise ArtifactLimitError(
            f"thread already has {ARTIFACT_MAX_COUNT_PER_THREAD} published artifacts"
        )
    existing_bytes = sum(int(item.get("size_bytes", 0)) for item in existing)
    if existing_bytes + len(content) > ARTIFACT_MAX_TOTAL_BYTES_PER_THREAD:
        raise ArtifactLimitError(
            "publishing this file would exceed the 50 MiB artifact limit for this thread"
        )

    artifact_id = uuid.uuid4().hex
    created_at = datetime.now(UTC)
    chunks = [
        content[offset : offset + ARTIFACT_CHUNK_BYTES]
        for offset in range(0, len(content), ARTIFACT_CHUNK_BYTES)
    ]
    if not chunks:
        chunks = [b""]
    chunk_namespace = _chunk_namespace(thread_id, artifact_id)
    attempted_keys: list[str] = []
    try:
        for index, chunk in enumerate(chunks):
            key = f"{index:06d}"
            attempted_keys.append(key)
            await _client().store.put_item(
                chunk_namespace,
                key,
                {"index": index, "data": base64.b64encode(chunk).decode("ascii")},
                index=False,
                ttl=ARTIFACT_TTL_MINUTES,
            )

        manifest = {
            "version": 1,
            "id": artifact_id,
            "filename": filename,
            "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "size_bytes": len(content),
            "sha256": digest,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(minutes=ARTIFACT_TTL_MINUTES)).isoformat(),
            "chunk_count": len(chunks),
            "chunk_size_bytes": ARTIFACT_CHUNK_BYTES,
            "source_path": source_path,
        }
        await _client().store.put_item(
            _manifest_namespace(thread_id),
            artifact_id,
            manifest,
            index=False,
            ttl=ARTIFACT_TTL_MINUTES,
        )
        return manifest
    except Exception:
        try:
            await _client().store.delete_item(_manifest_namespace(thread_id), artifact_id)
        except Exception:
            pass
        for key in attempted_keys:
            try:
                await _client().store.delete_item(chunk_namespace, key)
            except Exception:
                pass
        raise


async def read_thread_artifact(thread_id: str, artifact_id: str) -> tuple[dict[str, Any], bytes]:
    if not _ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
        raise ArtifactNotFoundError("artifact not found")
    item = await _client().store.get_item(
        _manifest_namespace(thread_id), artifact_id, refresh_ttl=False
    )
    manifest = _item_value(item)
    if manifest is None or not _valid_manifest(manifest):
        raise ArtifactNotFoundError("artifact not found")

    chunks: list[bytes] = []
    namespace = _chunk_namespace(thread_id, artifact_id)
    try:
        for index in range(int(manifest["chunk_count"])):
            chunk_item = await _client().store.get_item(
                namespace, f"{index:06d}", refresh_ttl=False
            )
            value = _item_value(chunk_item)
            encoded = value.get("data") if value else None
            if not isinstance(encoded, str):
                raise ArtifactCorruptError("artifact data is incomplete")
            chunks.append(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise ArtifactCorruptError("artifact data is corrupt") from exc

    content = b"".join(chunks)
    if len(content) != manifest["size_bytes"]:
        raise ArtifactCorruptError("artifact size does not match its manifest")
    if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
        raise ArtifactCorruptError("artifact checksum does not match its manifest")
    return manifest, content


async def delete_thread_artifacts(thread_id: str) -> None:
    for manifest in await list_thread_artifacts(thread_id):
        artifact_id = manifest["id"]
        namespace = _chunk_namespace(thread_id, artifact_id)
        for index in range(int(manifest["chunk_count"])):
            try:
                await _client().store.delete_item(namespace, f"{index:06d}")
            except Exception:
                pass
        try:
            await _client().store.delete_item(_manifest_namespace(thread_id), artifact_id)
        except Exception:
            pass


def _valid_manifest(manifest: dict[str, Any]) -> bool:
    return (
        manifest.get("version") == 1
        and isinstance(manifest.get("id"), str)
        and bool(_ARTIFACT_ID_PATTERN.fullmatch(manifest["id"]))
        and isinstance(manifest.get("filename"), str)
        and bool(manifest["filename"])
        and isinstance(manifest.get("mime_type"), str)
        and isinstance(manifest.get("size_bytes"), int)
        and 0 <= manifest["size_bytes"] <= ARTIFACT_MAX_BYTES
        and isinstance(manifest.get("sha256"), str)
        and bool(_SHA256_PATTERN.fullmatch(manifest["sha256"]))
        and isinstance(manifest.get("created_at"), str)
        and isinstance(manifest.get("expires_at"), str)
        and isinstance(manifest.get("chunk_count"), int)
        and 1 <= manifest["chunk_count"] <= (ARTIFACT_MAX_BYTES // ARTIFACT_CHUNK_BYTES) + 1
        and manifest.get("chunk_size_bytes") == ARTIFACT_CHUNK_BYTES
    )
