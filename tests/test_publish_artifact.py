from __future__ import annotations

import base64
import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.tools import publish_artifact as publish_artifact_tool

publish_artifact_module = importlib.import_module("agent.tools.publish_artifact")


def _read_local_chunk(
    workspace: Path,
    file_path: Path,
    *,
    max_bytes: int,
    chunk_bytes: int = 4,
    offset: int = 0,
    expected_sha256: str = "",
    expected_size: int = -1,
) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            publish_artifact_module._BOUNDED_FILE_CHUNK_SCRIPT,
            str(file_path),
            str(workspace),
            str(max_bytes),
            str(chunk_bytes),
            str(offset),
            expected_sha256,
            str(expected_size),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


class FakeBackend:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.execute_calls: list[tuple[str, int | None]] = []

    async def aexecute(self, command: str, *, timeout: int | None = None) -> Any:
        self.execute_calls.append((command, timeout))
        if not self.responses:
            raise AssertionError("unexpected sandbox execute call")
        return SimpleNamespace(
            output=json.dumps(self.responses.pop(0)),
            exit_code=0,
            truncated=False,
        )


class LocalBackend:
    async def aexecute(self, command: str, *, timeout: int | None = None) -> Any:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return SimpleNamespace(
            output=result.stdout,
            exit_code=result.returncode,
            truncated=False,
        )


def _chunk_responses(path: str, content: bytes, chunk_bytes: int) -> list[dict[str, Any]]:
    digest = hashlib.sha256(content).hexdigest()
    offsets = range(0, max(len(content), 1), chunk_bytes)
    return [
        {
            "ok": True,
            "path": path,
            "size": len(content),
            "sha256": digest,
            "offset": offset,
            "data": base64.b64encode(content[offset : offset + chunk_bytes]).decode("ascii"),
            "is_symlink": False,
        }
        for offset in offsets
    ]


def _patch_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    *,
    work_dir: str = "/workspace",
) -> None:
    async def fake_get_backend(thread_id: str) -> FakeBackend:
        assert thread_id == "thread-1"
        return backend

    async def fake_work_dir(candidate: FakeBackend) -> str:
        assert candidate is backend
        return work_dir

    monkeypatch.setattr(publish_artifact_module, "get_sandbox_backend", fake_get_backend)
    monkeypatch.setattr(publish_artifact_module, "aresolve_sandbox_work_dir", fake_work_dir)


async def test_publish_from_sandbox_persists_the_validated_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publish_artifact_module, "_TRANSFER_CHUNK_BYTES", 8)
    content = b"<html>done</html>"
    backend = FakeBackend(_chunk_responses("/workspace/output/report.html", content, 8))
    _patch_backend(monkeypatch, backend)
    captured: dict[str, Any] = {}

    async def fake_publish(
        thread_id: str,
        *,
        source_path: str,
        content: bytes,
        display_name: str | None,
    ) -> dict[str, Any]:
        captured.update(
            thread_id=thread_id,
            source_path=source_path,
            content=content,
            display_name=display_name,
        )
        return {"id": "a" * 32}

    monkeypatch.setattr(publish_artifact_module, "publish_thread_artifact", fake_publish)

    manifest = await publish_artifact_module._publish_from_sandbox(
        "thread-1",
        file_path="output/report.html",
        file_name="final-report.html",
    )

    assert manifest == {"id": "a" * 32}
    assert captured == {
        "thread_id": "thread-1",
        "source_path": "/workspace/output/report.html",
        "content": content,
        "display_name": "final-report.html",
    }
    assert len(backend.execute_calls) == 3
    assert all(timeout == 30 for _, timeout in backend.execute_calls)
    assert all("adownload" not in command for command, _ in backend.execute_calls)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {
                "ok": False,
                "error": "unsafe_path",
                "is_symlink": False,
            },
            "inside the sandbox workspace",
        ),
        (
            {
                "ok": False,
                "error": "unsafe_path",
                "is_symlink": True,
            },
            "symbolic links",
        ),
    ],
)
async def test_publish_from_sandbox_rejects_unsafe_paths(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, Any],
    message: str,
) -> None:
    backend = FakeBackend([response])
    _patch_backend(monkeypatch, backend)

    with pytest.raises(ValueError, match=message):
        await publish_artifact_module._publish_from_sandbox(
            "thread-1", file_path="output/report.html", file_name=None
        )


async def test_publish_from_sandbox_rejects_file_changed_during_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publish_artifact_module, "_TRANSFER_CHUNK_BYTES", 4)
    backend = FakeBackend(
        [
            *_chunk_responses("/workspace/output/report.html", b"abcdefgh", 4)[:1],
            {
                "ok": False,
                "error": "file_changed",
                "is_symlink": False,
            },
        ]
    )
    _patch_backend(monkeypatch, backend)

    with pytest.raises(ValueError, match="file changed"):
        await publish_artifact_module._publish_from_sandbox(
            "thread-1", file_path="output/report.html", file_name=None
        )


@pytest.mark.parametrize(
    "file_path",
    [
        ".open-swe/attachments/source.md",
        ".open-swe/deliverables/.credentials.json",
        ".open-swe/deliverables/.private/report.html",
        ".git/config.json",
        ".env.production",
        "output/credentials.json",
    ],
)
async def test_publish_from_sandbox_rejects_sensitive_paths_before_read(
    monkeypatch: pytest.MonkeyPatch,
    file_path: str,
) -> None:
    backend = FakeBackend([])
    _patch_backend(monkeypatch, backend)

    with pytest.raises(ValueError, match="credential and hidden configuration"):
        await publish_artifact_module._publish_from_sandbox(
            "thread-1", file_path=file_path, file_name=None
        )

    assert backend.execute_calls == []


async def test_publish_from_sandbox_allows_controlled_delivery_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"<html>done</html>"
    path = "/workspace/.open-swe/deliverables/report.html"
    backend = FakeBackend(_chunk_responses(path, content, 512 * 1024))
    _patch_backend(monkeypatch, backend)

    async def fake_publish(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"id": "a" * 32}

    monkeypatch.setattr(publish_artifact_module, "publish_thread_artifact", fake_publish)

    manifest = await publish_artifact_module._publish_from_sandbox(
        "thread-1",
        file_path=".open-swe/deliverables/report.html",
        file_name=None,
    )

    assert manifest == {"id": "a" * 32}


async def test_publish_from_sandbox_rejects_chunk_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(
        [
            {
                "ok": True,
                "path": "/workspace/output/report.html",
                "size": 5,
                "sha256": hashlib.sha256(b"other").hexdigest(),
                "offset": 0,
                "data": base64.b64encode(b"short").decode("ascii"),
                "is_symlink": False,
            }
        ]
    )
    _patch_backend(monkeypatch, backend)

    with pytest.raises(ValueError, match="checksum"):
        await publish_artifact_module._publish_from_sandbox(
            "thread-1", file_path="output/report.html", file_name=None
        )


async def test_publish_from_sandbox_rejects_oversize_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(
        [
            {
                "ok": False,
                "error": "too_large",
                "is_symlink": False,
            }
        ]
    )
    _patch_backend(monkeypatch, backend)

    with pytest.raises(ValueError, match="artifact limit"):
        await publish_artifact_module._publish_from_sandbox(
            "thread-1", file_path="output/report.html", file_name=None
        )


async def test_publish_artifact_requires_thread_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish_artifact_module, "get_config", lambda: {})

    result = await publish_artifact_tool("output/report.html")

    assert result == {"success": False, "error": "no thread_id in run config"}


async def test_bounded_host_reader_roundtrips_multiple_chunks_and_quoted_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publish_artifact_module, "_TRANSFER_CHUNK_BYTES", 4)
    source = tmp_path / "report with 'quote'.html"
    source.write_bytes(b"abcdefghij")

    content, resolved_path = await publish_artifact_module._read_bounded_sandbox_file(
        LocalBackend(),
        file_path=str(source),
        work_dir=str(tmp_path),
    )

    assert content == b"abcdefghij"
    assert resolved_path == str(source)


async def test_bounded_host_reader_handles_empty_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty.html"
    source.write_bytes(b"")

    content, resolved_path = await publish_artifact_module._read_bounded_sandbox_file(
        LocalBackend(),
        file_path=str(source),
        work_dir=str(tmp_path),
    )

    assert content == b""
    assert resolved_path == str(source)


def test_bounded_chunk_script_reads_only_the_requested_chunk(tmp_path: Path) -> None:
    source = tmp_path / "report.html"
    source.write_bytes(b"report")

    first = _read_local_chunk(tmp_path, source, max_bytes=10)
    second = _read_local_chunk(
        tmp_path,
        source,
        max_bytes=10,
        offset=4,
        expected_sha256=first["sha256"],
        expected_size=first["size"],
    )

    assert first["ok"] is True
    assert first["size"] == 6
    assert base64.b64decode(first["data"]) == b"repo"
    assert base64.b64decode(second["data"]) == b"rt"


def test_bounded_chunk_script_rejects_symlink_and_oversize(tmp_path: Path) -> None:
    source = tmp_path / "report.html"
    source.write_bytes(b"report")
    symlink = tmp_path / "linked.html"
    symlink.symlink_to(source)

    linked = _read_local_chunk(tmp_path, symlink, max_bytes=10)
    oversized = _read_local_chunk(tmp_path, source, max_bytes=3)

    assert linked["ok"] is False
    assert linked["is_symlink"] is True
    assert oversized["ok"] is False
    assert oversized["error"] == "too_large"


def test_bounded_chunk_script_detects_changes_between_chunks(tmp_path: Path) -> None:
    source = tmp_path / "report.html"
    source.write_bytes(b"first")
    initial = _read_local_chunk(tmp_path, source, max_bytes=10)
    source.write_bytes(b"other")

    changed = _read_local_chunk(
        tmp_path,
        source,
        max_bytes=10,
        offset=4,
        expected_sha256=initial["sha256"],
        expected_size=initial["size"],
    )

    assert changed == {"ok": False, "error": "file_changed", "is_symlink": False}
