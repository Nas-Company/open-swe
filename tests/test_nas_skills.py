from __future__ import annotations

from dataclasses import dataclass

import pytest
from deepagents.backends.protocol import ExecuteResponse

from agent.utils.nas_skills import materialize_nas_skills


@dataclass
class UploadResult:
    path: str
    error: str | None = None


class FakeBackend:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.uploads: list[tuple[str, bytes]] = []
        self.commands: list[str] = []

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.commands.append(command)
        return ExecuteResponse(output="", exit_code=0, truncated=False)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[UploadResult]:
        self.uploads = files
        return [UploadResult(path=path, error=self.error) for path, _ in files]


class FakeDictBackend(FakeBackend):
    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[dict[str, str | None]]:
        self.uploads = files
        return [{"path": path, "error": self.error} for path, _ in files]


class FakeIncompleteBackend(FakeBackend):
    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[UploadResult]:
        self.uploads = files
        return []


@pytest.mark.asyncio
async def test_materialize_nas_skills_uploads_bundle_and_version_marker() -> None:
    backend = FakeBackend()

    sources = await materialize_nas_skills(backend, "/workspace")  # type: ignore[arg-type]

    paths = [path for path, _ in backend.uploads]
    assert sources == ["/workspace/.open-swe/skills/"]
    assert paths == sorted(paths)
    assert "/workspace/.open-swe/skills/.bundle-sha256" in paths


@pytest.mark.asyncio
async def test_materialize_nas_skills_creates_upload_directories_first() -> None:
    backend = FakeBackend()

    await materialize_nas_skills(backend, "/workspace")  # type: ignore[arg-type]

    assert backend.commands == [
        "mkdir -p /workspace/.open-swe/skills "
        "/workspace/.open-swe/skills/product-live-issue-debugger "
        "/workspace/.open-swe/skills/product-live-issue-debugger/references"
    ]


@pytest.mark.asyncio
async def test_materialize_nas_skills_accepts_dictionary_upload_responses() -> None:
    sources = await materialize_nas_skills(FakeDictBackend(), "/workspace")  # type: ignore[arg-type]

    assert sources == ["/workspace/.open-swe/skills/"]


@pytest.mark.asyncio
async def test_materialize_nas_skills_rejects_incomplete_upload_responses() -> None:
    with pytest.raises(RuntimeError, match="incomplete file upload response"):
        await materialize_nas_skills(FakeIncompleteBackend(), "/workspace")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_materialize_nas_skills_rejects_upload_errors() -> None:
    backend = FakeBackend("permission_denied")

    with pytest.raises(RuntimeError, match="permission_denied"):
        await materialize_nas_skills(backend, "/workspace")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_product_live_issue_skill_routes_only_deployed_capabilities() -> None:
    backend = FakeBackend()

    await materialize_nas_skills(backend, "/workspace")  # type: ignore[arg-type]

    uploads = dict(backend.uploads)
    skill_text = uploads[
        "/workspace/.open-swe/skills/product-live-issue-debugger/SKILL.md"
    ].decode()
    assert "name: product-live-issue-debugger" in skill_text
    assert "query_aws_cloudwatch_logs" in skill_text
    assert "get_stripe_billing_context" in skill_text
    assert "search_code" in skill_text
    assert "get_admin_portal_record" in skill_text
    assert "## Copy-Ready Product Live Issues Reply" in skill_text
    assert "MongoDB" in skill_text and "unavailable" in skill_text
    assert (
        "/workspace/.open-swe/skills/product-live-issue-debugger/references/incident-report-template.md"
        in uploads
    )
    assert (
        "/workspace/.open-swe/skills/product-live-issue-debugger/references/production-mutation-gate.md"
        in uploads
    )
