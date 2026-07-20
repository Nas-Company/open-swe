from __future__ import annotations

import hashlib
import posixpath
import shlex
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import SandboxBackendProtocol

BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "nas_skills"
SANDBOX_SKILLS_DIR = ".open-swe/skills"


def _response_value(response: Any, field: str) -> Any:
    if isinstance(response, dict):
        return response.get(field)
    return getattr(response, field, None)


def bundled_skill_uploads(work_dir: str) -> list[tuple[str, bytes]]:
    root = posixpath.join(work_dir, SANDBOX_SKILLS_DIR)
    uploads: list[tuple[str, bytes]] = []
    digest = hashlib.sha256()

    for source in sorted(path for path in BUNDLED_SKILLS_DIR.rglob("*") if path.is_file()):
        relative = source.relative_to(BUNDLED_SKILLS_DIR).as_posix()
        content = source.read_bytes()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
        uploads.append((posixpath.join(root, relative), content))

    uploads.append((posixpath.join(root, ".bundle-sha256"), digest.hexdigest().encode()))
    return sorted(uploads, key=lambda item: item[0])


async def materialize_nas_skills(
    backend: SandboxBackendProtocol,
    work_dir: str,
) -> list[str]:
    uploads = bundled_skill_uploads(work_dir)
    directories = sorted({posixpath.dirname(path) for path, _ in uploads})
    mkdir_result = await backend.aexecute(
        "mkdir -p " + " ".join(shlex.quote(directory) for directory in directories)
    )
    if mkdir_result.exit_code != 0:
        detail = mkdir_result.output.strip() or f"exit code {mkdir_result.exit_code}"
        raise RuntimeError(f"Failed to materialize NAS skills: mkdir failed: {detail}")

    responses = await backend.aupload_files(uploads)
    if len(responses) != len(uploads):
        raise RuntimeError("Failed to materialize NAS skills: incomplete file upload response")
    errors = []
    for (path, _), response in zip(uploads, responses, strict=True):
        error = _response_value(response, "error")
        if error:
            response_path = _response_value(response, "path") or path
            errors.append(f"{response_path}: {error}")
    if errors:
        raise RuntimeError("Failed to materialize NAS skills: " + "; ".join(errors))
    return [posixpath.join(work_dir, SANDBOX_SKILLS_DIR) + "/"]
