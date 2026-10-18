"""Facebook Page tools: authorization guide + authorized Page listing."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ...workflow.orchestrator import CreatorService
from ..schemas import ToolEnvelope, guard

TOOL_CONNECT_FACEBOOK = "creator_connect_facebook"
TOOL_LIST_PAGES = "creator_list_pages"


def register_facebook_tools(server: MCPServer, service: CreatorService) -> None:
    @server.tool(
        name=TOOL_CONNECT_FACEBOOK,
        description=(
            "Return the official Meta authorization guide (exact steps, docs URLs, required "
            "environment variables) and, when a token is already configured, verify the token "
            "identity WITHOUT publishing anything. The agent never collects Facebook passwords. "
            "Use it when the user needs to connect or check their Facebook Page setup; do not use "
            "it to publish (that is creator_publish_video with explicit approval)."
        ),
    )
    async def creator_connect_facebook() -> ToolEnvelope:
        return await guard(TOOL_CONNECT_FACEBOOK, service.connect_facebook())

    @server.tool(
        name=TOOL_LIST_PAGES,
        description=(
            "List the Facebook Pages the configured token can manage, with granted tasks and "
            "publish capability. Performs a read-only Graph API request; requires "
            "FCA_FACEBOOK_ACCESS_TOKEN. Use it to pick a page_id for the review/approval flow; "
            "it never posts or modifies anything."
        ),
    )
    async def creator_list_pages() -> ToolEnvelope:
        return await guard(TOOL_LIST_PAGES, service.list_pages())
