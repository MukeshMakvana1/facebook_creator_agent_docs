"""Tool registration: all 18 ``creator_*`` tools across focused modules."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ...workflow.orchestrator import CreatorService
from .facebook_tools import register_facebook_tools
from .production import register_production_tools
from .review_tools import register_review_tools
from .setup import register_setup_tools

TOOL_NAMES = (
    # setup (3)
    "creator_doctor",
    "creator_list_providers",
    "creator_test_llm",
    # facebook (2)
    "creator_connect_facebook",
    "creator_list_pages",
    # production (8)
    "creator_create_project",
    "creator_generate_script",
    "creator_search_media",
    "creator_download_assets",
    "creator_generate_narration",
    "creator_build_timeline",
    "creator_render_preview",
    "creator_validate_render",
    # review/publish/jobs (5)
    "creator_get_review_package",
    "creator_approve_publish",
    "creator_publish_video",
    "creator_get_publish_status",
    "creator_cancel_job",
)


def register_all_tools(server: MCPServer, service: CreatorService) -> None:
    register_setup_tools(server, service)
    register_facebook_tools(server, service)
    register_production_tools(server, service)
    register_review_tools(server, service)
