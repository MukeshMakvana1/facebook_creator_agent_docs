"""MCP-level tests: spawn the real server over stdio and inspect the surface.

The server is started as a subprocess exactly the way an MCP client would
(``python -m facebook_creator_agent``), using the official SDK client for the
``initialize`` handshake, ``tools/list``, and ``tools/call``.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV_PYTHON if _VENV_PYTHON.exists() else __import__("sys").executable)


def _params(base_dir: Path):
    from mcp import StdioServerParameters

    env = dict(os.environ)
    env["FCA_BASE_DIR"] = str(base_dir)
    env["FCA_LOG_LEVEL"] = "ERROR"
    return StdioServerParameters(
        command=PYTHON,
        args=["-m", "facebook_creator_agent"],
        env=env,
        cwd=str(base_dir),
    )


def _structured(result):
    return getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )


async def test_stdio_boot_lists_all_18_tools_and_rejects_bad_args(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from facebook_creator_agent.mcp_server.tools import TOOL_NAMES

    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
            assert getattr(server_info, "name", None) == "facebook-creator-agent"

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 18

            # Missing required argument: the SDK rejects the call itself.
            bad = await session.call_tool("creator_create_project", {})
            assert bool(getattr(bad, "isError", False) or getattr(bad, "is_error", False))


async def test_semantic_failures_return_structured_envelopes(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool("creator_create_project", {"topic": "   "})
            envelope = _structured(result)
            assert isinstance(envelope, dict)
            assert envelope["status"] == "error"
            assert envelope["tool"] == "creator_create_project"

            error = envelope["error"]
            assert set(error) >= {
                "code",
                "safe_explanation",
                "retryable",
                "stage",
                "next_action",
            }
            assert error["code"] == "INVALID_INPUT"
            assert error["stage"] == "setup"
            assert isinstance(error["retryable"], bool)

            doctor = await session.call_tool("creator_doctor", {})
            doctor_envelope = _structured(doctor)
            assert doctor_envelope["status"] == "ok"
            assert doctor_envelope["data"]["overall"] in {"ready", "partial", "blocked"}
