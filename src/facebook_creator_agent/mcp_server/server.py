"""MCP server assembly: one MCPServer exposing the 18 ``creator_*`` tools.

The server holds no business logic (ARCHITECTURE.md §4.1); every tool lands in
:class:`facebook_creator_agent.workflow.orchestrator.CreatorService`.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.mcpserver import MCPServer

from ..config.settings import Settings, load_settings
from ..workflow.orchestrator import CreatorService, build_default_service
from .tools import TOOL_NAMES, register_all_tools

SERVER_NAME = "facebook-creator-agent"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "Facebook Creator Agent: turns a topic into a rendered Facebook Page video, with explicit "
    "user approval before any publication.\n"
    "\n"
    "Workflow order: creator_create_project -> creator_generate_script -> creator_search_media -> "
    "creator_download_assets -> creator_generate_narration -> creator_build_timeline -> "
    "creator_render_preview -> creator_get_review_package -> (user reviews) -> "
    "creator_approve_publish -> creator_publish_video -> creator_get_publish_status.\n"
    "\n"
    "Hard rules for callers:\n"
    "- Never call creator_approve_publish on your own judgement: the USER must explicitly approve "
    "the exact reviewed payload for the exact Page.\n"
    "- Publishing runs as a dry-run by default; a real upload requires FCA_ENABLE_LIVE_PUBLISH=1 "
    "and a configured token. Never retry an UNKNOWN upload outcome without first calling "
    "creator_get_publish_status.\n"
    "- Secrets (tokens/keys) live only in environment variables; never echo them into prompts, "
    "files, or logs.\n"
    "- Media is used only from allowlisted providers/licenses; user media requires explicit "
    "rights confirmation.\n"
    "\n"
    "Every tool returns a structured envelope with status, artifacts, warnings, next_action, and "
    "(on failure) a five-field error contract."
)


def build_server(
    service: Optional[CreatorService] = None, *, settings: Optional[Settings] = None
) -> MCPServer:
    """Build the MCP server bound to a CreatorService (created if omitted)."""
    if service is None:
        service = build_default_service(settings or load_settings())
    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
    )
    register_all_tools(server, service)
    return server


def run_server(
    settings: Optional[Settings] = None,
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = build_server(settings=settings)
    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "streamable-http":
        server.run(transport="streamable-http", host=host, port=port)
    else:  # pragma: no cover - cli validates first
        raise ValueError(f"Unsupported transport: {transport!r}")


__all__ = ["SERVER_NAME", "SERVER_VERSION", "TOOL_NAMES", "build_server", "run_server"]
