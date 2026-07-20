from __future__ import annotations

import hashlib
import posixpath
from pathlib import Path

from deepagents.backends.protocol import SandboxBackendProtocol

BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "nas_skills"
SANDBOX_SKILLS_DIR = ".open-swe/skills"


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
    responses = await backend.aupload_files(uploads)
    errors = [f"{response.path}: {response.error}" for response in responses if response.error]
    if errors:
        raise RuntimeError("Failed to materialize NAS skills: " + "; ".join(errors))
    return [posixpath.join(work_dir, SANDBOX_SKILLS_DIR) + "/"]
