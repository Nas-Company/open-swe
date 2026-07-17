"""Ensure terminal responses publish the final files they advertise."""

from __future__ import annotations

import json
import logging
import posixpath
import re
import shlex
from pathlib import PurePosixPath
from typing import Any, NotRequired
from urllib.parse import unquote
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

_MAX_AUTO_ARTIFACTS = 10
_DELIVERABLE_EXTENSIONS = (
    ".csv",
    ".docx",
    ".epub",
    ".html",
    ".htm",
    ".json",
    ".md",
    ".pdf",
    ".pptx",
    ".rtf",
    ".tar.gz",
    ".tgz",
    ".tsv",
    ".txt",
    ".xlsx",
    ".zip",
)
_DELIVERY_HEADINGS = frozenset(
    {
        "artifacts",
        "deliverables",
        "downloads",
        "files to download",
        "final deliverables",
        "final files",
        "generated files",
        "output files",
        "report files",
    }
)
_BLOCKED_PATH_PARTS = frozenset(
    {
        ".aws",
        ".config",
        ".git",
        ".gnupg",
        ".kube",
        ".qa",
        ".ssh",
        "node_modules",
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
_INLINE_CODE_RE = re.compile(r"`([^`\r\n]+)`")
_SANDBOX_PATH_RE = re.compile(r"sandbox:(?P<path>/[^)\]\r\n]+)")
_SANDBOX_MARKDOWN_LINK_RE = re.compile(r"\[[^\]\r\n]*\]\(sandbox:/[^)\r\n]+\)")
_TERMINAL_BLOCKERS = (
    "Model call limits exceeded",
    "Sandbox circuit breaker triggered",
)

_LIST_OUTBOX_SCRIPT = r"""
import json
import os
import stat
import sys

lexical_root = os.path.abspath(sys.argv[1])
paths = []
try:
    root_info = os.lstat(lexical_root)
except (FileNotFoundError, OSError):
    root_info = None
root = os.path.realpath(lexical_root)
if (
    root_info is not None
    and stat.S_ISDIR(root_info.st_mode)
    and not stat.S_ISLNK(root_info.st_mode)
    and root == lexical_root
):
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".") and not os.path.islink(os.path.join(directory, name))
        ]
        for filename in filenames:
            if filename.startswith("."):
                continue
            candidate = os.path.join(directory, filename)
            try:
                info = os.lstat(candidate)
                resolved = os.path.realpath(candidate)
                if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    if os.path.commonpath([root, resolved]) == root:
                        paths.append(resolved)
            except (FileNotFoundError, OSError, ValueError):
                continue
            if len(paths) >= 10:
                break
        if len(paths) >= 10:
            break
print(json.dumps(sorted(paths)))
""".strip()


class ArtifactDeliveryState(AgentState):
    plan_mode: NotRequired[bool]


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text", "")
            parts.append(text if isinstance(text, str) else str(text))
        else:
            parts.append(str(block))
    return "\n".join(parts)


def _filename_stems(filename: str) -> set[str]:
    stems: set[str] = set()
    remaining = filename.casefold()
    while "." in remaining:
        remaining = remaining.rsplit(".", 1)[0]
        stems.add(remaining)
    stems.add(remaining)
    return stems


def _normalize_artifact_path(
    raw_path: str,
    *,
    work_dir: str,
    repo_dir: str | None,
) -> str | None:
    candidate = raw_path.strip().strip("'\"")
    if candidate.startswith("sandbox:"):
        candidate = candidate.removeprefix("sandbox:")
    candidate = unquote(candidate)
    candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
    if not candidate or "\x00" in candidate or "\n" in candidate or "\r" in candidate:
        return None
    if "://" in candidate or any(char in candidate for char in ";|&$<>{}"):
        return None

    normalized_work_dir = posixpath.normpath(work_dir)
    normalized_repo_dir = posixpath.normpath(repo_dir) if repo_dir else None
    if posixpath.isabs(candidate):
        resolved = posixpath.normpath(candidate)
    else:
        relative = posixpath.normpath(candidate.removeprefix("./"))
        if relative in {"", ".", ".."} or relative.startswith("../"):
            return None
        base = normalized_repo_dir or normalized_work_dir
        if normalized_repo_dir:
            repo_name = posixpath.basename(normalized_repo_dir)
            if relative == repo_name or relative.startswith(f"{repo_name}/"):
                base = normalized_work_dir
        resolved = posixpath.normpath(posixpath.join(base, relative))

    try:
        if posixpath.commonpath([normalized_work_dir, resolved]) != normalized_work_dir:
            return None
    except ValueError:
        return None

    relative_parts = PurePosixPath(posixpath.relpath(resolved, normalized_work_dir)).parts
    is_outbox = relative_parts[:2] == (".open-swe", "deliverables")
    if relative_parts[:2] == (".open-swe", "attachments"):
        return None
    artifact_parts = relative_parts[2:] if is_outbox else relative_parts
    if any(part.startswith(".") for part in artifact_parts):
        return None
    if any(part.casefold() in _BLOCKED_PATH_PARTS for part in relative_parts):
        return None

    filename = PurePosixPath(resolved).name.casefold()
    if filename.startswith(".env") or not filename.endswith(_DELIVERABLE_EXTENSIONS):
        return None
    if _filename_stems(filename) & _BLOCKED_FILENAME_STEMS:
        return None
    return resolved


def _heading_is_delivery_context(heading: str) -> bool:
    normalized = heading.casefold().strip().rstrip(":")
    return normalized in _DELIVERY_HEADINGS


def extract_artifact_paths(
    text: str,
    *,
    work_dir: str,
    repo_dir: str | None = None,
    outbox_paths: tuple[str, ...] = (),
) -> list[str]:
    """Extract high-confidence final deliverable paths from a terminal response."""
    outbox_dir = posixpath.join(posixpath.normpath(work_dir), ".open-swe", "deliverables")
    raw_candidates: list[str] = []
    for outbox_path in outbox_paths:
        normalized_outbox_path = posixpath.normpath(outbox_path)
        try:
            inside_outbox = posixpath.commonpath([outbox_dir, normalized_outbox_path]) == outbox_dir
        except ValueError:
            inside_outbox = False
        if inside_outbox:
            raw_candidates.append(outbox_path)
    raw_candidates.extend(match.group("path").strip() for match in _SANDBOX_PATH_RE.finditer(text))
    inline_text = _SANDBOX_MARKDOWN_LINK_RE.sub("", text)

    heading = ""
    for line in inline_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
        if not _heading_is_delivery_context(heading):
            continue
        raw_candidates.extend(match.group(1).strip() for match in _INLINE_CODE_RE.finditer(line))

    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in raw_candidates:
        path = _normalize_artifact_path(raw_path, work_dir=work_dir, repo_dir=repo_dir)
        if path is None or path in seen:
            continue
        seen.add(path)
        paths.append(path)
        if len(paths) >= _MAX_AUTO_ARTIFACTS:
            break
    return paths


def _message_tool_calls(message: BaseMessage) -> list[dict[str, Any]]:
    calls = getattr(message, "tool_calls", None)
    return calls if isinstance(calls, list) else []


def _messages_since_last_human(messages: list[BaseMessage]) -> list[BaseMessage]:
    for index in range(len(messages) - 1, -1, -1):
        if getattr(messages[index], "type", "") == "human":
            return messages[index + 1 :]
    return messages


class ArtifactDeliveryMiddleware(AgentMiddleware[ArtifactDeliveryState, Any]):
    """Convert advertised final files into auditable publish tool calls."""

    state_schema = ArtifactDeliveryState

    def __init__(
        self,
        *,
        work_dir: str,
        repo_dir: str | None = None,
        sandbox_backend: Any | None = None,
        outbox_enabled: bool = True,
    ) -> None:
        self._work_dir = posixpath.normpath(work_dir)
        self._repo_dir = posixpath.normpath(repo_dir) if repo_dir else None
        self._sandbox_backend = sandbox_backend
        self._outbox_enabled = outbox_enabled
        self._outbox_dir = posixpath.join(self._work_dir, ".open-swe", "deliverables")

    async def _list_outbox(self) -> tuple[str, ...]:
        if not self._outbox_enabled or self._sandbox_backend is None:
            return ()
        command = " ".join(
            [
                "python -c",
                shlex.quote(_LIST_OUTBOX_SCRIPT),
                shlex.quote(self._outbox_dir),
            ]
        )
        try:
            result = await self._sandbox_backend.aexecute(command, timeout=30)
            if result.exit_code != 0 or getattr(result, "truncated", False):
                return ()
            payload = json.loads(result.output)
            if not isinstance(payload, list):
                return ()
            return tuple(path for path in payload if isinstance(path, str))
        except Exception:
            logger.warning("Failed to inspect the artifact delivery outbox", exc_info=True)
            return ()

    def _attempted_paths(self, messages: list[BaseMessage]) -> set[str]:
        attempted: set[str] = set()
        for message in _messages_since_last_human(messages):
            for call in _message_tool_calls(message):
                if call.get("name") != "publish_artifact":
                    continue
                args = call.get("args")
                raw_path = args.get("file_path") if isinstance(args, dict) else None
                if not isinstance(raw_path, str):
                    continue
                path = _normalize_artifact_path(
                    raw_path,
                    work_dir=self._work_dir,
                    repo_dir=None,
                )
                if path:
                    attempted.add(path)
        return attempted

    async def aafter_model(
        self,
        state: ArtifactDeliveryState,
        runtime: Runtime,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages or state.get("plan_mode") is True or state.get("jump_to") == "end":
            return None

        last_message = messages[-1]
        if not isinstance(last_message, AIMessage) or _message_tool_calls(last_message):
            return None
        text = _content_to_text(last_message.content).strip()
        if not text or any(marker in text for marker in _TERMINAL_BLOCKERS):
            return None

        candidates = extract_artifact_paths(
            text,
            work_dir=self._work_dir,
            repo_dir=self._repo_dir,
            outbox_paths=await self._list_outbox(),
        )
        attempted = self._attempted_paths(messages)
        pending = [path for path in candidates if path not in attempted]
        if not pending:
            return None

        last_message.tool_calls = [
            {
                "name": "publish_artifact",
                "args": {"file_path": path},
                "id": str(uuid4()),
                "type": "tool_call",
            }
            for path in pending
        ]
        return {"messages": [last_message]}
