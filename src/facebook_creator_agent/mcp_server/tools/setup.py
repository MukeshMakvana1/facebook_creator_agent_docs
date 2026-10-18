"""Setup and diagnostics tools: doctor, provider listing, LLM endpoint test."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ...workflow.orchestrator import CreatorService
from ..schemas import ToolEnvelope, guard

TOOL_DOCTOR = "creator_doctor"
TOOL_LIST_PROVIDERS = "creator_list_providers"
TOOL_TEST_LLM = "creator_test_llm"


def register_setup_tools(server: MCPServer, service: CreatorService) -> None:
    @server.tool(
        name=TOOL_DOCTOR,
        description=(
            "Check dependencies and configuration for the Facebook Creator Agent: Python version, "
            "ffmpeg/ffprobe availability, workspace writability, LLM/TTS/media/Facebook provider "
            "status, and recent-project resume consistency. Read-only; never contacts external "
            "services unless probe_network=true. Use this first when anything misbehaves; do not "
            "use it mid-workflow for progress (use the project-specific tools instead)."
        ),
    )
    async def creator_doctor(probe_network: bool = False) -> ToolEnvelope:
        return await guard(TOOL_DOCTOR, service.doctor(probe_network=probe_network))

    @server.tool(
        name=TOOL_LIST_PROVIDERS,
        description=(
            "Show the configured model, TTS, media, research, and Facebook providers with their "
            "capability status; secrets are always redacted. Read-only and offline (no network "
            "calls). Use it to explain what is available before a workflow; it does not test "
            "connectivity (use creator_test_llm for that)."
        ),
    )
    async def creator_list_providers() -> ToolEnvelope:
        return await guard(TOOL_LIST_PROVIDERS, service.list_providers())

    @server.tool(
        name=TOOL_TEST_LLM,
        description=(
            "Send a minimal test request to the configured OpenAI-compatible LLM endpoint and "
            "report latency/model/reply preview. Performs a network request to the user's own "
            "configured endpoint. Use it after changing LLM configuration; it is not needed when "
            "creator_doctor already reports the endpoint unreachable (fix config first)."
        ),
    )
    async def creator_test_llm() -> ToolEnvelope:
        return await guard(TOOL_TEST_LLM, service.test_llm())
