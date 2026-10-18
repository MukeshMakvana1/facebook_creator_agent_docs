"""Command-line entry point.

* ``facebook-creator-agent`` / ``python -m facebook_creator_agent`` — run the
  MCP server (stdio by default, which is what MCP clients spawn).
* ``... doctor`` — run the diagnostics and print the JSON report to stdout
  (exit 0 ready/partial, 2 blocked, 1 configuration failure).
* ``... tools`` — print the registered tool names (offline sanity check).
* ``... print-config`` — print the redacted, effective configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

from .config.settings import load_settings
from .core.errors import CreatorError
from .mcp_server.server import SERVER_NAME, SERVER_VERSION, build_server
from .mcp_server.tools import TOOL_NAMES
from .observability.logging import configure_logging
from .workflow.orchestrator import build_default_service


def _load(transport_args: argparse.Namespace):
    settings = load_settings(base_dir=transport_args.base_dir)
    configure_logging(settings.log_level)
    return settings


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="facebook-creator-agent",
        description=f"{SERVER_NAME} {SERVER_VERSION} — MCP server for the Facebook Creator Agent workflow.",
    )
    parser.add_argument("--base-dir", default=None, help="Base directory for config/workspace (default: cwd).")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server (default).")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    doctor = sub.add_parser("doctor", help="Run diagnostics and print a JSON report.")
    doctor.add_argument("--probe-network", action="store_true", help="Also test the LLM endpoint.")

    sub.add_parser("tools", help="List the registered tool names and exit.")
    sub.add_parser("print-config", help="Print the redacted effective configuration.")

    args = parser.parse_args(argv)

    try:
        settings = _load(args)
    except CreatorError as exc:
        _print_json({"status": "error", "error": exc.to_info().model_dump()})
        return 1

    if args.command == "doctor":
        service = build_default_service(settings)
        result = asyncio.run(service.doctor(probe_network=getattr(args, "probe_network", False)))
        report = {
            "status": result["data"]["overall"],
            "components": result["data"]["components"],
            "config_issues": result["data"]["config_issues"],
        }
        _print_json(report)
        return 2 if report["status"] == "blocked" else 0

    if args.command == "tools":
        _print_json({"server": SERVER_NAME, "version": SERVER_VERSION, "count": len(TOOL_NAMES), "tools": list(TOOL_NAMES)})
        return 0

    if args.command == "print-config":
        _print_json(settings.redacted_dump())
        return 0

    # default: serve
    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8765)
    server = build_server(settings=settings)  # fail fast on registration errors
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="streamable-http", host=host, port=port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
