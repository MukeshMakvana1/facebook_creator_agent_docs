"""Boot verification for the Facebook Creator Agent MCP server.

Spawns the server as a real MCP stdio client (official ``mcp`` SDK), performs the
``initialize`` handshake, lists tools, and probes two error paths. Prints a JSON
report and exits non-zero when a check fails.

Usage (from the repository root)::

    .venv\\Scripts\\python.exe scripts\\verify_mcp_boot.py

Options::

    --python PATH     Interpreter to spawn (default: .venv\\Scripts\\python.exe)
    --module NAME     Module to run (default: facebook_creator_agent)
    --base-dir PATH   FCA_BASE_DIR for the child process (default: repo root)
    --json            Print the raw JSON report only (no log noise on success)

This script performs no network calls and never publishes anywhere; it only
exercises the local stdio protocol surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOL_COUNT = 18


async def _run(python: str, module: str, base_dir: str) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["FCA_BASE_DIR"] = base_dir

    params = StdioServerParameters(
        command=python,
        args=["-m", module],
        env=env,
        cwd=base_dir,
    )

    report: dict[str, Any] = {"checks": []}

    def check(name: str, ok: bool, details: Any = None) -> None:
        report["checks"].append({"check": name, "ok": ok, "details": details})

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_info = getattr(init, "serverInfo", None) or getattr(init, "server_info", None)
            report["server"] = {
                "name": getattr(server_info, "name", None),
                "version": getattr(server_info, "version", None),
                "protocol": getattr(init, "protocolVersion", None) or getattr(init, "protocol_version", None),
            }
            check("initialize", server_info is not None, report["server"])

            tools_result = await session.list_tools()
            names = sorted(t.name for t in tools_result.tools)
            report["tool_count"] = len(names)
            report["tools"] = names
            check("tools/list count == %d" % EXPECTED_TOOL_COUNT, len(names) == EXPECTED_TOOL_COUNT, len(names))
            check("all tools start with creator_", all(n.startswith("creator_") for n in names))

            # Probe 1: missing required argument -> SDK-level validation error.
            try:
                bad = await session.call_tool("creator_create_project", {})
                structured = getattr(bad, "structuredContent", None) or getattr(bad, "structured_content", None)
                is_error = bool(getattr(bad, "isError", False) or getattr(bad, "is_error", False))
                check("invalid args (missing topic) reported as error", is_error or bool(structured), structured or "(no structured content)")
            except Exception as exc:  # SDK may raise ToolError for argument validation
                check("invalid args (missing topic) reported as error", True, f"raised {type(exc).__name__}")

            # Probe 2: semantic validation -> structured creator error envelope.
            probe = await session.call_tool("creator_create_project", {"topic": "   "})
            structured = getattr(probe, "structuredContent", None) or getattr(probe, "structured_content", None)
            error_code = None
            if isinstance(structured, dict):
                error_code = (structured.get("error") or {}).get("code")
            check(
                "empty topic -> structured INVALID_INPUT",
                isinstance(structured, dict) and error_code == "INVALID_INPUT" and structured.get("status") == "error",
                error_code,
            )

    report["ok"] = all(c["ok"] for c in report["checks"])
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the Facebook Creator Agent MCP server boots over stdio.")
    parser.add_argument("--python", default=str(REPO_ROOT / ".venv" / "Scripts" / "python.exe"))
    parser.add_argument("--module", default="facebook_creator_agent")
    parser.add_argument("--base-dir", default=str(REPO_ROOT))
    parser.add_argument("--json", action="store_true", help="Print only the JSON report.")
    args = parser.parse_args(argv)

    python = args.python
    if not Path(python).exists():
        print(f"python not found: {python}", file=sys.stderr)
        return 3

    try:
        report = asyncio.run(_run(python, args.module, args.base_dir))
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    if report.get("ok") and not args.json:
        print("\nBOOT VERIFICATION: PASS", file=sys.stderr)
    elif not report.get("ok"):
        print("\nBOOT VERIFICATION: FAIL", file=sys.stderr)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
