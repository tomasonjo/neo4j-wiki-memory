"""Neo4j-backed persistent agent memory, exposed as a FastMCP server."""

from .memory import register

__all__ = ["register"]
