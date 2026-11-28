"""`lemmata-mcp <server>` runs one MCP server on stdio.

lemmata-mcp lean | library | compute | verdict | whiteboard | lean_lsp
lemmata-mcp freeze spec.json          freeze a challenge (operator)
lemmata-mcp lts {search,show,brief,import}  browse Left to Solve and import challenges
lemmata-mcp triage <id> [--budget-s 120] [--json]  advisory pre-run check (operator)
lemmata-mcp goals {show,snapshot,prompt}    goal-tree state and round prompts (round runner)
lemmata-mcp build-index decls.jsonl   build the declaration index (setup)
lemmata-mcp smoke [server ...]        start each server over stdio and list its tools
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

from .common import HOME, OPTIONS, SCRATCH, WORK_PROJECT, base_env, sandboxed

SERVERS = {
    "lean": "lean_server",
    "library": "library_server",
    "compute": "compute_server",
    "verdict": "verdict_server",
    "whiteboard": "board",
}

# lean-lsp-mcp tools that need the network or rebuild the project: disabled at the source.
LEAN_LSP_DISABLED = [
    "lean_build",
    "lean_leansearch",
    "lean_leanfinder",
    "lean_hammer_premise",
    "lean_state_search",
]


def lean_lsp() -> None:
    """Exec lean-lsp-mcp on the work project, sandboxed so Lean code it elaborates can write
    only the work project, its own caches and scratch. One warm REPL is the lean server's,
    so lean-lsp-mcp's REPL mode stays off; Loogle is local only. LeanSearch stays disabled
    unless LEANSEARCH=1 in options.env (and it is allowlisted in the research profile)."""
    disabled = [
        t for t in LEAN_LSP_DISABLED if t != "lean_leansearch" or OPTIONS["LEANSEARCH"] != "1"
    ]
    loogle_cache = HOME / "loogle"
    env = base_env(
        LEAN_PROJECT_PATH=str(WORK_PROJECT),
        LEAN_LOG_LEVEL="NONE",
        LEAN_LOOGLE_LOCAL="true",
        LEAN_LOOGLE_LOCAL_ONLY="true",
        LEAN_LOOGLE_CACHE_DIR=str(loogle_cache),
        LEAN_MCP_DISABLED_TOOLS=",".join(disabled),
    )
    binary = str(Path(sys.executable).parent / "lean-lsp-mcp")
    cmd = sandboxed([binary], env, [WORK_PROJECT, loogle_cache, SCRATCH, Path.home() / ".cache"])
    os.execvpe(cmd[0], cmd, env)


async def _smoke(names: list[str]) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    for name in names:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "lemmata_mcp", name], env={**os.environ}
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"{name}: {len(tools)} tools")
            for tool in tools:
                print(f"  {tool.name}")


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else ""
    if command in SERVERS:
        module = importlib.import_module(f"lemmata_mcp.{SERVERS[command]}")
        module.server.run()
    elif command == "lean_lsp":
        lean_lsp()
    elif command == "freeze" and len(args) == 2:
        from .verdict_server import freeze_cli

        freeze_cli(args[1])
    elif command == "lts":
        from .left_to_solve import main as lts_main

        lts_main(args[1:])
    elif command == "triage":
        from .triage import main as triage_main

        triage_main(args[1:])
    elif command == "goals":
        from .goals_cli import main as goals_main

        goals_main(args[1:])
    elif command == "build-index" and len(args) == 2:
        from .library_server import build_index

        build_index(Path(args[1]))
    elif command == "smoke":
        asyncio.run(_smoke(args[1:] or [*SERVERS, "lean_lsp"]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
