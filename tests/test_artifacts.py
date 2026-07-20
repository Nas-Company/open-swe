from __future__ import annotations

import asyncio
import base64
import hashlib
from typing import Any

import pytest

from agent.dashboard import artifacts


class FakeStore:
    def __init__(self) -> None:
        self.items: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.delete_calls: list[tuple[tuple[str, ...], str]] = []
        self.fail_manifest_put = False
        self.commit_then_fail_kind: str | None = None
        self._commit_failure_raised = False

    async def put_item(
        self,
        namespace: list[str],
        key: str,
        value: dict[str, Any],
        *,
        index: bool | None = None,
        ttl: int | None = None,
    ) -> None:
        namespace_key = tuple(namespace)
        self.put_calls.append(
            {
                "namespace": namespace_key,
                "key": key,
                "value": value,
                "index": index,
                "ttl": ttl,
            }
        )
        if self.fail_manifest_put and namespace_key[:2] == ("thread_artifacts", "manifest"):
            raise RuntimeError("manifest store unavailable")
        self.items[(namespace_key, key)] = {"value": value}
        kind = namespace_key[1] if namespace_key[:1] == ("thread_artifacts",) else None
        if self.commit_then_fail_kind == kind and not self._commit_failure_raised:
            self._commit_failure_raised = True
            raise RuntimeError("store timeout after commit")

    async def get_item(
        self,
        namespace: list[str],
        key: str,
        *,
        refresh_ttl: bool | None = None,
    ) -> dict[str, Any] | None:
        namespace_key = tuple(namespace)
        self.get_calls.append({"namespace": namespace_key, "key": key, "refresh_ttl": refresh_ttl})
        return self.items.get((namespace_key, key))

    async def search_items(
        self,
        namespace: list[str],
        *,
        limit: int,
        refresh_ttl: bool | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        namespace_key = tuple(namespace)
        self.search_calls.append(
            {
                "namespace": namespace_key,
                "limit": limit,
                "refresh_ttl": refresh_ttl,
            }
        )
        matches = [
            item
            for (item_namespace, _), item in self.items.items()
            if item_namespace == namespace_key
        ]
        return {"items": matches[:limit]}

    async def delete_item(self, namespace: list[str], key: str) -> None:
        namespace_key = tuple(namespace)
        self.delete_calls.append((namespace_key, key))
        self.items.pop((namespace_key, key), None)


class FakeClient:
    def __init__(self, store: FakeStore) -> None:
        self.store = store


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    store = FakeStore()
    client = FakeClient(store)
    monkeypatch.setattr(artifacts, "_client", lambda: client)
    return store


async def test_artifact_roundtrip_chunks_bytes_and_applies_ttl(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts, "ARTIFACT_CHUNK_BYTES", 4)
    content = b"\x00abc\xffdefghi"

    manifest = await artifacts.publish_thread_artifact(
        "thread-1",
        source_path="/workspace/output/report.html",
        content=content,
        display_name="report.html",
    )
    stored_manifest, downloaded = await artifacts.read_thread_artifact("thread-1", manifest["id"])

    assert downloaded == content
    assert stored_manifest == manifest
    assert manifest["version"] == 1
    assert manifest["chunk_count"] == 3
    assert manifest["chunk_size_bytes"] == 4
    assert manifest["mime_type"] == "text/html"
    assert manifest["sha256"] == hashlib.sha256(content).hexdigest()
    assert all(call["index"] is False for call in fake_store.put_calls)
    assert all(call["ttl"] == artifacts.ARTIFACT_TTL_MINUTES for call in fake_store.put_calls)
    assert all(call["refresh_ttl"] is False for call in fake_store.search_calls)
    assert all(call["refresh_ttl"] is False for call in fake_store.get_calls)


async def test_artifact_publish_rolls_back_chunks_when_manifest_write_fails(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts, "ARTIFACT_CHUNK_BYTES", 2)
    fake_store.fail_manifest_put = True

    with pytest.raises(RuntimeError, match="manifest store unavailable"):
        await artifacts.publish_thread_artifact(
            "thread-1",
            source_path="/workspace/output/report.html",
            content=b"abcdef",
        )

    assert fake_store.items == {}
    chunk_puts = [
        call
        for call in fake_store.put_calls
        if call["namespace"][:2] == ("thread_artifacts", "chunks")
    ]
    assert len(chunk_puts) == 3
    chunk_namespace = chunk_puts[0]["namespace"]
    manifest_put = next(
        call
        for call in fake_store.put_calls
        if call["namespace"][:2] == ("thread_artifacts", "manifest")
    )
    assert fake_store.delete_calls == [
        (("thread_artifacts", "manifest", "thread-1"), manifest_put["key"]),
        *[(chunk_namespace, f"{index:06d}") for index in range(len(chunk_puts))],
    ]


@pytest.mark.parametrize("failure_kind", ["chunks", "manifest"])
async def test_artifact_publish_rolls_back_items_committed_before_client_timeout(
    fake_store: FakeStore,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    monkeypatch.setattr(artifacts, "ARTIFACT_CHUNK_BYTES", 2)
    fake_store.commit_then_fail_kind = failure_kind

    with pytest.raises(RuntimeError, match="store timeout after commit"):
        await artifacts.publish_thread_artifact(
            "thread-1",
            source_path="/workspace/output/report.html",
            content=b"abcdef",
        )

    assert fake_store.items == {}


async def test_artifact_read_rejects_corrupt_chunk(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts, "ARTIFACT_CHUNK_BYTES", 4)
    manifest = await artifacts.publish_thread_artifact(
        "thread-1",
        source_path="/workspace/output/report.html",
        content=b"abcdefgh",
    )
    namespace = tuple(artifacts._chunk_namespace("thread-1", manifest["id"]))
    fake_store.items[(namespace, "000001")] = {"value": {"index": 1, "data": "not valid base64!"}}

    with pytest.raises(artifacts.ArtifactCorruptError, match="corrupt"):
        await artifacts.read_thread_artifact("thread-1", manifest["id"])


async def test_artifact_read_rejects_checksum_mismatch(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts, "ARTIFACT_CHUNK_BYTES", 8)
    manifest = await artifacts.publish_thread_artifact(
        "thread-1",
        source_path="/workspace/output/report.html",
        content=b"abcdefgh",
    )
    namespace = tuple(artifacts._chunk_namespace("thread-1", manifest["id"]))
    fake_store.items[(namespace, "000000")] = {
        "value": {"index": 0, "data": base64.b64encode(b"abcdEfgh").decode("ascii")}
    }

    with pytest.raises(artifacts.ArtifactCorruptError, match="checksum"):
        await artifacts.read_thread_artifact("thread-1", manifest["id"])


async def test_parallel_publishes_cannot_bypass_thread_count_limit(
    fake_store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifacts, "ARTIFACT_MAX_COUNT_PER_THREAD", 1)

    results = await asyncio.gather(
        artifacts.publish_thread_artifact(
            "thread-1", source_path="/workspace/one.html", content=b"one"
        ),
        artifacts.publish_thread_artifact(
            "thread-1", source_path="/workspace/two.html", content=b"two"
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, artifacts.ArtifactLimitError) for result in results) == 1
    assert len(await artifacts.list_thread_artifacts("thread-1")) == 1
