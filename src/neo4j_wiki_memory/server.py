"""Standalone FastMCP server exposing the Neo4j-backed wiki memory tools.

Run with:
    neo4j-wiki-memory                       # stdio transport (default)
    neo4j-wiki-memory --transport http      # streamable HTTP transport
    python -m neo4j_wiki_memory

Required env vars:
    NEO4J_MEMORY_URI
    NEO4J_MEMORY_USERNAME
    NEO4J_MEMORY_PASSWORD
    NEO4J_MEMORY_WIKI      (optional, defaults to "default")
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from fastmcp import FastMCP

from . import memory


def build_server() -> FastMCP:
    if not memory.memory_enabled():
        missing = [
            v
            for v in ("NEO4J_MEMORY_URI", "NEO4J_MEMORY_USERNAME", "NEO4J_MEMORY_PASSWORD")
            if not os.getenv(v)
        ]
        raise RuntimeError(
            "Missing required env vars: " + ", ".join(missing)
        )

    mcp = FastMCP(
        name="neo4j-wiki-memory",
        instructions=(
            "Persistent agent memory backed by a Neo4j wiki. Pages are "
            "markdown, linked by [[wikilinks]]. Check search_memory / "
            "list_memories at the start of a task before assuming you have "
            "no prior context."
        ),
    )
    memory.register(mcp)
    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="neo4j-wiki-memory",
        description="FastMCP server for Neo4j-backed agent memory.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "sse"),
        default="stdio",
        help="Transport to serve (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind for http/sse transports (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind for http/sse transports (default: 8000).",
    )
    args = parser.parse_args()

    try:
        mcp = build_server()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        if args.transport == "stdio":
            mcp.run()
        else:
            mcp.run(transport=args.transport, host=args.host, port=args.port)
    finally:
        # Best-effort driver shutdown so we don't leak the Bolt pool.
        try:
            asyncio.run(memory._close_driver())
        except RuntimeError:
            # Event loop already closed (e.g. when FastMCP ran its own).
            pass


if __name__ == "__main__":
    main()
