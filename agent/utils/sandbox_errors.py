"""Provider-neutral sandbox error classification."""

from __future__ import annotations

try:
    from langsmith.sandbox import SandboxClientError
except Exception:  # pragma: no cover - defensive for optional provider imports
    SandboxClientError = ()  # type: ignore[assignment]


def is_retryable_sandbox_connection_error(exc: Exception) -> bool:
    """Return true when a sandbox failure should trigger sandbox recreation."""
    if isinstance(exc, SandboxClientError):
        return True

    class_name = exc.__class__.__name__
    module = exc.__class__.__module__
    message = str(exc).lower()

    if class_name == "NotFoundError" and (
        module.startswith("modal")
        or "sandbox" in message
        or "container" in message
    ):
        return "not found" in message or "shut down" in message

    if module.startswith("modal") and (
        "no container with id" in message
        or "already shut down" in message
        or "sandbox has already shut down" in message
    ):
        return True

    return False
