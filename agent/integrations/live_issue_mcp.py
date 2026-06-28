"""Server-side Production Live Issues tools backed by LIIS MCP.

Credentials are read from environment variables and attached as an
``Authorization: Bearer ...`` header to the MCP connection, which runs in the
LangGraph server process. The sandbox never holds LIIS credentials.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

DEFAULT_LIVE_ISSUE_MCP_URL = "https://liveissueinvestigationservice.dev-nas.com/mcp"
_LIVE_ISSUE_HOST = "liveissueinvestigationservice.dev-nas.com"
_LIVE_ISSUE_PATH = "/mcp"
_MCP_TIMEOUT_SECONDS = 30.0
_TOKEN_ENV_NAMES = (
    "LIIS_TOKEN",
    "LIVE_ISSUE_INVESTIGATION_TOKEN",
    "LIIS_MCP_TOKEN",
    "LIVE_ISSUE_MCP_TOKEN",
)
_TOKEN_QUERY_PARAMS = frozenset({"token", "api_key"})
_URL_ENV_NAMES = (
    "LIIS_MCP_URL",
    "LIVE_ISSUE_INVESTIGATION_MCP_URL",
    "LIVE_ISSUE_MCP_URL",
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ALLOWED_TOOL_NAMES = frozenset(
    {
        "get_lark_thread_context",
        "download_lark_message_image",
        "list_aws_resources",
        "get_aws_service_observability",
        "query_aws_cloudwatch_logs",
        "list_aws_s3_objects",
        "get_stripe_customer",
        "get_stripe_payment",
        "get_stripe_subscription",
        "get_stripe_billing_context",
        "list_stripe_products_and_prices",
        "list_stripe_events",
        "tail_stripe_api_logs",
        "list_stripe_radar_rules",
        "find_stripe_failed_payments_by_email",
        "list_code_repositories",
        "search_code",
        "get_code_context",
        "read_code_file_lines",
        "find_code_files",
        "get_code_repository_overview",
        "get_admin_portal_session_status",
        "list_admin_portal_resources",
        "describe_admin_portal_collection",
        "list_admin_portal_records",
        "get_admin_portal_record",
    }
)


@dataclass(frozen=True)
class LiveIssueMCPConfig:
    url: str
    token: str


def _first_env_value(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _extract_token_from_query(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    token = ""
    for name in _TOKEN_QUERY_PARAMS:
        values = query.pop(name, [])
        if not token:
            token = next((value.strip() for value in values if value.strip()), "")
    cleaned_query = urlencode(query, doseq=True)
    cleaned_url = urlunparse(parsed._replace(query=cleaned_query))
    return token, cleaned_url


def _is_live_issue_mcp_url(url: str) -> bool:
    parsed = urlparse(url)
    path_matches = parsed.path.rstrip("/") == _LIVE_ISSUE_PATH
    if not path_matches:
        return False
    if parsed.scheme == "https" and parsed.hostname == _LIVE_ISSUE_HOST:
        return True
    return parsed.scheme in {"http", "https"} and parsed.hostname in _LOCAL_HOSTS


def load_live_issue_mcp_config() -> LiveIssueMCPConfig | None:
    """Return LIIS MCP config when the environment contains valid settings."""
    url = _first_env_value(_URL_ENV_NAMES) or DEFAULT_LIVE_ISSUE_MCP_URL
    token = _first_env_value(_TOKEN_ENV_NAMES)
    query_token, url = _extract_token_from_query(url)
    if not token:
        token = query_token
    if not token:
        return None
    if not _is_live_issue_mcp_url(url):
        logger.warning("Ignoring LIIS MCP config with unsupported URL: %s", url)
        return None
    return LiveIssueMCPConfig(url=url, token=token)


async def _build_mcp_tools(config: LiveIssueMCPConfig) -> list[BaseTool]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
        {
            "live_issue": {
                "transport": "streamable_http",
                "url": config.url,
                "headers": {
                    "Authorization": f"Bearer {config.token}",
                },
                "timeout": timedelta(seconds=_MCP_TIMEOUT_SECONDS),
            }
        }
    )
    return await client.get_tools()


async def load_live_issue_tools() -> list[BaseTool]:
    """Return allowed LIIS MCP tools when configured, else ``[]``."""
    config = load_live_issue_mcp_config()
    if config is None:
        return []
    try:
        tools = await _build_mcp_tools(config)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to load LIIS MCP tools", exc_info=True)
        return []
    allowed_tools = [tool for tool in tools if tool.name in _ALLOWED_TOOL_NAMES]
    logger.info(
        "Loaded %d LIIS MCP tool(s), exposing %d allowed tool(s)",
        len(tools),
        len(allowed_tools),
    )
    return allowed_tools
