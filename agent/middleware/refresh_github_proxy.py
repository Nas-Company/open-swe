"""Before-model middleware that keeps sandbox GitHub authentication fresh.

GitHub App installation tokens expire after one hour. Long LangSmith or Modal
runs would otherwise hit 401s on every ``gh``/``git`` call. This hook refreshes
provider-specific auth before each model call when the token is near expiry.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentState, before_model
from langgraph.config import get_config
from langgraph.runtime import Runtime

from ..utils.github_proxy import maybe_refresh_proxy_token

logger = logging.getLogger(__name__)


@before_model
async def refresh_github_proxy_before_model(
    state: AgentState,  # noqa: ARG001
    runtime: Runtime,  # noqa: ARG001
) -> dict[str, Any] | None:
    """Refresh the sandbox GitHub token before it expires mid-run."""
    try:
        config = get_config()
        thread_id = config.get("configurable", {}).get("thread_id")
    except Exception:  # noqa: BLE001
        return None

    if not thread_id:
        return None

    try:
        await maybe_refresh_proxy_token(thread_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to refresh sandbox GitHub token for thread %s",
            thread_id,
            exc_info=True,
        )
    return None
