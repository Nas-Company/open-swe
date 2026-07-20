from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest
from fastapi import HTTPException

from agent.dashboard import artifacts, routes, thread_api


def _manifest() -> dict[str, Any]:
    return {
        "version": 1,
        "id": "a" * 32,
        "filename": "report.html",
        "mime_type": "text/html",
        "size_bytes": 6,
        "sha256": "b" * 64,
        "created_at": "2026-07-17T00:00:00+00:00",
        "expires_at": "2026-08-16T00:00:00+00:00",
        "chunk_count": 1,
        "chunk_size_bytes": artifacts.ARTIFACT_CHUNK_BYTES,
        "source_path": "/workspace/output/report.html",
    }


async def test_list_thread_artifacts_checks_read_access_before_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_called = False

    async def deny_thread(
        thread_id: str, login: str, *, email: str | None = None
    ) -> dict[str, Any]:
        raise HTTPException(404, "thread not found")

    async def fake_list(thread_id: str) -> list[dict[str, Any]]:
        nonlocal store_called
        store_called = True
        return []

    monkeypatch.setattr(thread_api, "_readable_thread", deny_thread)
    monkeypatch.setattr(thread_api, "list_thread_artifacts", fake_list)

    with pytest.raises(HTTPException) as exc_info:
        await thread_api.list_dashboard_thread_artifacts("thread-1", "intruder")

    assert exc_info.value.status_code == 404
    assert store_called is False


async def test_list_thread_artifacts_returns_only_public_manifest_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def allow_thread(
        thread_id: str, login: str, *, email: str | None = None
    ) -> dict[str, Any]:
        return {"thread_id": thread_id, "metadata": {"github_login": login}}

    async def fake_list(thread_id: str) -> list[dict[str, Any]]:
        assert thread_id == "thread-1"
        return [_manifest()]

    monkeypatch.setattr(thread_api, "_readable_thread", allow_thread)
    monkeypatch.setattr(thread_api, "list_thread_artifacts", fake_list)

    result = await thread_api.list_dashboard_thread_artifacts("thread-1", "owner")

    assert result == {
        "artifacts": [
            {
                "id": "a" * 32,
                "fileName": "report.html",
                "mimeType": "text/html",
                "sizeBytes": 6,
                "sha256": "b" * 64,
                "createdAt": "2026-07-17T00:00:00+00:00",
                "expiresAt": "2026-08-16T00:00:00+00:00",
            }
        ]
    }
    assert "sourcePath" not in result["artifacts"][0]


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (artifacts.ArtifactNotFoundError("missing"), 404),
        (artifacts.ArtifactCorruptError("corrupt"), 410),
        (RuntimeError("store unavailable"), 502),
    ],
)
async def test_get_thread_artifact_normalizes_storage_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status_code: int,
) -> None:
    async def allow_thread(
        thread_id: str, login: str, *, email: str | None = None
    ) -> dict[str, Any]:
        return {"thread_id": thread_id, "metadata": {"github_login": login}}

    async def fail_read(thread_id: str, artifact_id: str) -> tuple[dict[str, Any], bytes]:
        raise error

    monkeypatch.setattr(thread_api, "_readable_thread", allow_thread)
    monkeypatch.setattr(thread_api, "read_thread_artifact", fail_read)

    with pytest.raises(HTTPException) as exc_info:
        await thread_api.get_dashboard_thread_artifact("thread-1", "a" * 32, "owner")

    assert exc_info.value.status_code == status_code


def test_content_disposition_safely_encodes_unicode_and_header_characters() -> None:
    filename = '季度报告 "final"\r\nX-Evil.html'

    value = artifacts.artifact_content_disposition(filename)

    assert value.startswith('attachment; filename="')
    assert "\r" not in value
    assert "\n" not in value
    assert "季度报告" not in value
    assert f"filename*=UTF-8''{quote(filename, safe='')}" in value


async def test_download_route_forces_attachment_security_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = {**_manifest(), "filename": "季度报告.html"}

    async def fake_get(
        thread_id: str,
        artifact_id: str,
        login: str,
        *,
        email: str | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        assert (thread_id, artifact_id, login, email) == (
            "thread-1",
            "a" * 32,
            "owner",
            "owner@example.com",
        )
        return manifest, b"report"

    monkeypatch.setattr(routes, "get_dashboard_thread_artifact", fake_get)

    response = await routes.api_download_thread_artifact(
        "thread-1",
        "a" * 32,
        session={"sub": "owner", "email": "owner@example.com"},
    )

    assert response.body == b"report"
    assert response.media_type == "text/html"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]


async def test_thread_delete_cleans_artifacts_after_deleting_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeThreads:
        async def get(self, thread_id: str) -> dict[str, Any]:
            return {
                "thread_id": thread_id,
                "metadata": {"source": "dashboard", "github_login": "owner"},
            }

        async def delete(self, thread_id: str) -> None:
            events.append(f"delete:{thread_id}")

    class FakeClient:
        threads = FakeThreads()

    async def fake_cleanup(thread_id: str) -> None:
        events.append(f"cleanup:{thread_id}")

    monkeypatch.setattr(thread_api, "langgraph_client", lambda: FakeClient())
    monkeypatch.setattr(thread_api, "delete_thread_artifacts", fake_cleanup)

    await thread_api.delete_dashboard_thread("thread-1", "owner")

    assert events == ["delete:thread-1", "cleanup:thread-1"]


async def test_thread_delete_failure_preserves_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_called = False

    class FakeThreads:
        async def get(self, thread_id: str) -> dict[str, Any]:
            return {
                "thread_id": thread_id,
                "metadata": {"source": "dashboard", "github_login": "owner"},
            }

        async def delete(self, thread_id: str) -> None:
            raise RuntimeError("thread store unavailable")

    class FakeClient:
        threads = FakeThreads()

    async def fake_cleanup(thread_id: str) -> None:
        nonlocal cleanup_called
        cleanup_called = True

    monkeypatch.setattr(thread_api, "langgraph_client", lambda: FakeClient())
    monkeypatch.setattr(thread_api, "delete_thread_artifacts", fake_cleanup)

    with pytest.raises(RuntimeError, match="thread store unavailable"):
        await thread_api.delete_dashboard_thread("thread-1", "owner")

    assert cleanup_called is False
