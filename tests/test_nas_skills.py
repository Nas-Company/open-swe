from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent.utils.nas_skills import materialize_nas_skills


@dataclass
class UploadResult:
    path: str
    error: str | None = None


class FakeBackend:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.uploads: list[tuple[str, bytes]] = []

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[UploadResult]:
        self.uploads = files
        return [UploadResult(path=path, error=self.error) for path, _ in files]


@pytest.mark.asyncio
async def test_materialize_nas_skills_uploads_bundle_and_version_marker() -> None:
    backend = FakeBackend()

    sources = await materialize_nas_skills(backend, "/workspace")  # type: ignore[arg-type]

    paths = [path for path, _ in backend.uploads]
    assert sources == ["/workspace/.open-swe/skills/"]
    assert paths == sorted(paths)
    assert "/workspace/.open-swe/skills/.bundle-sha256" in paths


@pytest.mark.asyncio
async def test_materialize_nas_skills_rejects_upload_errors() -> None:
    backend = FakeBackend("permission_denied")

    with pytest.raises(RuntimeError, match="permission_denied"):
        await materialize_nas_skills(backend, "/workspace")  # type: ignore[arg-type]
