"""Persistent agent memory backed by Neo4j, exposed as a FastMCP server.

Models a markdown-style wiki where each page is a `Page` node and
`[[wikilinks]]` become `LINKS_TO` relationships. All pages are scoped to a
single `wiki` namespace (configured via NEO4J_MEMORY_WIKI, default
"default").

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
from typing import Any

from fastmcp import FastMCP
from neo4j import AsyncDriver, AsyncGraphDatabase

_driver: AsyncDriver | None = None
_schema_ready = False
WIKI = os.getenv("NEO4J_MEMORY_WIKI", "default")
FULLTEXT_ANALYZER = "standard-folding"


def _normalize(target: str) -> str:
    """Normalize a wikilink target into a canonical page path.

    Returns an empty string for targets that should not produce an edge
    (empty, all-whitespace, trailing slash). Callers must skip empty
    results.
    """
    target = target.strip()
    if not target or target.endswith("/"):
        return ""
    if not target.endswith(".md"):
        target += ".md"
    return target


def _extract_links(content: str) -> list[str]:
    """Extract wikilink targets from markdown content.

    Context-aware: skips fenced code blocks (```...``` and ~~~...~~~)
    and inline code spans (`...`, ``...``). Honors backslash escapes —
    `\\[`, `\\]`, `` \\` `` and `\\\\` each consume their following
    character, so `\\[\\[foo\\]\\]` in prose does not create an edge.

    Wikilinks do not span newlines: an unclosed `[[` stops at the next
    newline and is ignored. Nested `[[` is rejected — `[[[x]]]` yields
    no link. Alias syntax `[[target|alias]]` is supported; the alias is
    ignored for graph purposes.
    """
    if not content:
        return []
    seen: set[str] = set()
    out: list[str] = []
    i = 0
    n = len(content)
    while i < n:
        c = content[i]

        if c == "\\" and i + 1 < n:
            i += 2
            continue

        if (c == "`" or c == "~") and content[i : i + 3] == c * 3:
            fence = c * 3
            close = content.find(fence, i + 3)
            if close == -1:
                break
            i = close + 3
            continue

        if c == "`":
            run = 0
            while i + run < n and content[i + run] == "`":
                run += 1
            delim = "`" * run
            close = content.find(delim, i + run)
            if close == -1:
                i += run
                continue
            i = close + run
            continue

        if c == "[" and i + 1 < n and content[i + 1] == "[":
            j = i + 2
            end = -1
            while j < n:
                ch = content[j]
                if ch == "\n":
                    break
                if ch == "\\" and j + 1 < n:
                    j += 2
                    continue
                if ch == "[":
                    break
                if ch == "]":
                    if j + 1 < n and content[j + 1] == "]":
                        end = j
                    break
                j += 1
            if end == -1:
                i += 2
                continue
            inner = content[i + 2 : end]
            target_part = inner.split("|", 1)[0]
            t = _normalize(target_part)
            if t and t not in seen:
                seen.add(t)
                out.append(t)
            i = end + 2
            continue

        i += 1
    return out


def _iso(value: Any) -> Any:
    """Render a Neo4j DateTime to an ISO-8601 string; pass None through."""
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def memory_enabled() -> bool:
    return all(
        os.getenv(v)
        for v in ("NEO4J_MEMORY_URI", "NEO4J_MEMORY_USERNAME", "NEO4J_MEMORY_PASSWORD")
    )


async def _get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        uri = os.environ["NEO4J_MEMORY_URI"]
        user = os.environ["NEO4J_MEMORY_USERNAME"]
        password = os.environ["NEO4J_MEMORY_PASSWORD"]
        _driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    return _driver


async def _close_driver() -> None:
    global _driver, _schema_ready
    if _driver is not None:
        await _driver.close()
        _driver = None
        _schema_ready = False


async def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    driver = await _get_driver()
    await driver.execute_query(
        "CREATE CONSTRAINT page_wiki_path IF NOT EXISTS "
        "FOR (p:Page) REQUIRE (p.wiki, p.path) IS UNIQUE"
    )
    # Ensure the fulltext index uses the desired analyzer. Accent /
    # case folding (café ↔ cafe) needs `standard-folding`; the default
    # `standard` analyzer is accent-sensitive. Existing installs on
    # the old analyzer are migrated by drop+recreate.
    existing = await driver.execute_query(
        "SHOW INDEXES YIELD name, options "
        "WHERE name = 'page_fulltext' "
        "RETURN options"
    )
    needs_recreate = False
    if existing.records:
        options = existing.records[0]["options"] or {}
        index_config = options.get("indexConfig", {}) or {}
        if index_config.get("fulltext.analyzer") != FULLTEXT_ANALYZER:
            needs_recreate = True
    if needs_recreate:
        await driver.execute_query("DROP INDEX page_fulltext IF EXISTS")
    await driver.execute_query(
        "CREATE FULLTEXT INDEX page_fulltext IF NOT EXISTS "
        "FOR (p:Page) ON EACH [p.path, p.content] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: $analyzer}}",
        analyzer=FULLTEXT_ANALYZER,
    )
    _schema_ready = True


# --- tool implementations ------------------------------------------------


async def read_memory(path: str, include_backlinks: bool = False) -> Any:
    """Read a stored memory page from your agentic memory. Use this to recall
    what you've previously learned and saved about a topic, person, or the
    user before answering. Memory persists across sessions — always check
    here for relevant context.

    Pass `include_backlinks=True` to also return the paths of every page
    that links to this one (inbound `LINKS_TO` edges). Tombstoned sources
    are filtered out. Useful for seeing where an entity or concept has
    come up across your memory.

    Args:
        path: Memory page path, e.g. "user/profile.md".
        include_backlinks: If True, include a `backlinks` list in the
            response with paths of pages linking to this one.
    """
    await _ensure_schema()
    path = _normalize(path)
    if not path:
        return {"error": True, "message": "path is empty"}
    driver = await _get_driver()
    if include_backlinks:
        result = await driver.execute_query(
            "MATCH (p:Page {wiki: $wiki, path: $path}) "
            "WHERE coalesce(p.deleted, false) = false "
            "OPTIONAL MATCH (other:Page {wiki: $wiki})-[:LINKS_TO]->(p) "
            "  WHERE coalesce(other.deleted, false) = false "
            "WITH p, other ORDER BY other.path "
            "RETURN p.content AS content, "
            "       [x IN collect(other.path) WHERE x IS NOT NULL] AS backlinks",
            wiki=WIKI, path=path,
        )
        if not result.records:
            return {"error": True, "message": f"Page not found: {path}"}
        record = result.records[0]
        return {
            "path": path,
            "content": record["content"] or "",
            "backlinks": list(record["backlinks"]),
        }
    result = await driver.execute_query(
        "MATCH (p:Page {wiki: $wiki, path: $path}) "
        "WHERE coalesce(p.deleted, false) = false "
        "RETURN p.content AS content",
        wiki=WIKI, path=path,
    )
    if not result.records:
        return {"error": True, "message": f"Page not found: {path}"}
    return {"path": path, "content": result.records[0]["content"] or ""}


async def write_memory(path: str, content: str) -> Any:
    """Save or overwrite a memory in your agentic memory. Use this whenever
    you want to remember something for later: facts about the user
    (preferences, goals, background, working style), insights from
    conversations, decisions made, patterns you've noticed, or any concept
    worth recalling in future sessions. Parses `[[wikilinks]]` from content
    and links to those memories, auto-creating empty stubs as needed.

    The parser is context-aware: `[[...]]` inside fenced code blocks
    (```` ``` ```` / `~~~`) and inline code spans (`` ` ``, `` `` ``)
    is ignored, and backslash-escaped brackets (`\\[\\[foo\\]\\]`) are
    suppressed so examples in prose don't create stubs. Links do not
    span newlines; empty `[[]]` is rejected.

    Writing a page replaces its content and recomputes its outbound edge
    set. If any linked target is currently tombstoned (soft-deleted),
    the response includes `linked_to_deleted` listing those paths — the
    edges are created, but the target stays deleted until written to
    directly.

    Recommended page layout — organise memories by topic, not by
    conversation, so they can be recalled independently of when they
    were learned. Group facts and learnings about each *database* and
    each *agent* under their own page so they can be retrieved
    individually rather than scanning a single mixed log:

        user/profile.md          # who they are, role, responsibilities
        user/preferences.md      # tooling, style, do / don't

        databases/<dbid>.md      # one page per Aura database. Capture:
                                 #   - purpose / dataset description
                                 #   - schema quirks, label & rel naming
                                 #   - known-good Cypher patterns
                                 #   - gotchas, slow queries, indexes
                                 #   - links to related agents/concepts
        databases/<dbid>/<topic>.md  # optional sub-pages for deep dives
                                 #   (e.g. databases/abc123/schema.md)

        agents/<agent_id>.md     # one page per agent. Capture:
                                 #   - what it's for, who uses it
                                 #   - tool list and why each was chosen
                                 #   - prompt-engineering lessons
                                 #   - failure modes, fixes, retries
                                 #   - link to its `databases/<dbid>.md`
        agents/<agent_id>/<topic>.md # optional sub-pages

        entities/<name>.md       # people, orgs, services, repos
        concepts/<name>.md       # domain ideas worth knowing
        learnings/<topic>.md     # cross-cutting lessons not tied to one
                                 # database or agent
        log.md                   # scratch / chronological notes

    The `databases/` and `agents/` namespaces are recommendations, not
    rules — feel free to add other top-level folders when something
    doesn't fit. The point is that anything you learn about a *specific*
    database or agent should live on its own page (keyed by id), so a
    future session can call `read_memory("databases/<dbid>.md")` or
    `read_memory("agents/<agent_id>.md")` and get exactly that context
    instead of having to grep a mixed log.

    Cross-link liberally with `[[wikilinks]]` — every agent page should
    link to its `[[databases/<dbid>]]`, learnings should link to the
    `[[concepts/...]]` they relate to, and so on. Every link becomes a
    graph edge you can traverse later via `read_memory(path,
    include_backlinks=True)`. Prefer refining an existing page over
    creating near-duplicates.

    Args:
        path: Memory page path, e.g. "user/profile.md".
        content: Full markdown content of the memory.
    """
    await _ensure_schema()
    path = _normalize(path)
    if not path:
        return {"error": True, "message": "path is empty"}
    links = _extract_links(content)
    driver = await _get_driver()
    # Content write, link teardown, and link rebuild are one statement so
    # they commit atomically.
    result = await driver.execute_query(
        "MERGE (p:Page {wiki: $wiki, path: $path}) "
        "ON CREATE SET p.created_at = datetime() "
        "SET p.content = $content, p.deleted = false, "
        "    p.size = size($content), p.updated_at = datetime() "
        "WITH p "
        "CALL { "
        "  WITH p "
        "  MATCH (p)-[r:LINKS_TO]->() "
        "  DELETE r "
        "} "
        "WITH p "
        "CALL { "
        "  WITH p "
        "  UNWIND $links AS target "
        "  MERGE (t:Page {wiki: $wiki, path: target}) "
        "  ON CREATE SET t.content = '', t.deleted = false, "
        "                t.size = 0, t.created_at = datetime(), "
        "                t.updated_at = datetime() "
        "  MERGE (p)-[:LINKS_TO]->(t) "
        "  WITH t WHERE coalesce(t.deleted, false) = true "
        "  RETURN collect(t.path) AS linked "
        "} "
        "RETURN linked AS linked_to_deleted",
        wiki=WIKI, path=path, content=content, links=links,
    )
    linked_to_deleted = (
        list(result.records[0]["linked_to_deleted"]) if result.records else []
    )
    response: dict[str, Any] = {"ok": True, "path": path, "links": links}
    if linked_to_deleted:
        # Surface edges created to tombstoned pages. They are real edges
        # in the graph but the target won't resurrect until someone
        # writes at its path — the caller needs to know.
        response["linked_to_deleted"] = linked_to_deleted
    return response


async def append_memory(path: str, content: str) -> Any:
    """Append to an existing memory without rewriting it. Use for running
    logs (`log.md`), timelines on an entity, or accumulating observations
    about the user over time. Adds new links for any wikilinks in the
    appended text. Upserts the page if it doesn't exist.

    The parser only scans the appended chunk, so `added_links` is the
    delta — existing outbound edges are untouched. Same parser rules as
    `write_memory`: code blocks and backslash-escaped brackets are
    ignored. If any linked target is tombstoned, the response includes
    `linked_to_deleted`.

    Not safe to blind-retry on timeout: if the server succeeded but the
    client never got the response, a retry double-appends. Read the page
    or check `updated_at` before retrying.

    Args:
        path: Memory page path, e.g. "log.md".
        content: Markdown to append. A newline is inserted before it if the
            existing memory does not already end with one.
    """
    await _ensure_schema()
    path = _normalize(path)
    if not path:
        return {"error": True, "message": "path is empty"}
    links = _extract_links(content)
    driver = await _get_driver()
    result = await driver.execute_query(
        "MERGE (p:Page {wiki: $wiki, path: $path}) "
        "ON CREATE SET p.content = '', p.deleted = false, "
        "              p.created_at = datetime() "
        "WITH p, CASE "
        "  WHEN coalesce(p.content, '') = '' THEN $content "
        "  WHEN right(p.content, 1) = '\n' THEN p.content + $content "
        "  ELSE p.content + '\n' + $content END AS new_content "
        "SET p.deleted = false, "
        "    p.content = new_content, "
        "    p.size = size(new_content), "
        "    p.updated_at = datetime() "
        "WITH p "
        "CALL { "
        "  WITH p "
        "  UNWIND $links AS target "
        "  MERGE (t:Page {wiki: $wiki, path: target}) "
        "  ON CREATE SET t.content = '', t.deleted = false, "
        "                t.size = 0, t.created_at = datetime(), "
        "                t.updated_at = datetime() "
        "  MERGE (p)-[:LINKS_TO]->(t) "
        "  WITH t WHERE coalesce(t.deleted, false) = true "
        "  RETURN collect(t.path) AS linked "
        "} "
        "RETURN linked AS linked_to_deleted",
        wiki=WIKI, path=path, content=content, links=links,
    )
    linked_to_deleted = (
        list(result.records[0]["linked_to_deleted"]) if result.records else []
    )
    response: dict[str, Any] = {"ok": True, "path": path, "added_links": links}
    if linked_to_deleted:
        response["linked_to_deleted"] = linked_to_deleted
    return response


_LIST_SORT_FIELDS = {"path", "updated_at", "created_at", "size"}


async def list_memories(
    prefix: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    sort_by: str | None = None,
    order: str | None = None,
) -> Any:
    """List memory pages, with pagination, sorting, and metadata. Use to
    browse what you already remember in a category (e.g. `entities/` to see
    everyone you've tracked, `user/` for what you know about the user) or
    to find recently-updated pages with `sort_by="updated_at"`,
    `order="desc"`.

    All arguments are optional. Each entry includes `path`, `size` (bytes
    of content), `created_at`, and `updated_at` (ISO-8601).

    Args:
        prefix: Optional path prefix to filter by. Defaults to "" (all).
        limit: Maximum number of results to return. Defaults to 10.
        offset: Number of results to skip. Defaults to 0.
        sort_by: One of "path", "updated_at", "created_at", "size".
            Defaults to "path".
        order: "asc" or "desc". Defaults to "asc".
    """
    if sort_by is not None and sort_by not in _LIST_SORT_FIELDS:
        return {
            "error": True,
            "message": f"sort_by must be one of {sorted(_LIST_SORT_FIELDS)}",
        }
    order_norm = order.lower() if order is not None else "asc"
    if order_norm not in ("asc", "desc"):
        return {"error": True, "message": "order must be 'asc' or 'desc'"}
    if limit is not None and limit < 1:
        return {"error": True, "message": "limit must be >= 1"}
    if offset is not None and offset < 0:
        return {"error": True, "message": "offset must be >= 0"}

    sort_field = sort_by or "path"
    # Sort field is whitelisted above, so direct interpolation is safe and
    # avoids the Cypher "cannot parameterise ORDER BY" limitation.
    direction = "DESC" if order_norm == "desc" else "ASC"
    # Inner subquery paginates; outer always emits exactly one row so
    # `total` is reported correctly even when offset > total.
    cypher = (
        "MATCH (p:Page {wiki: $wiki}) "
        "WHERE coalesce(p.deleted, false) = false "
        "  AND p.path STARTS WITH coalesce($prefix, '') "
        "WITH count(p) AS total, collect(p) AS pages "
        "CALL { "
        "  WITH pages "
        "  UNWIND pages AS p "
        "  WITH p "
        f"  ORDER BY p.{sort_field} {direction}, p.path ASC "
        "  SKIP coalesce($offset, 0) LIMIT coalesce($limit, 10) "
        "  RETURN collect({path: p.path, size: p.size, "
        "                  created_at: p.created_at, "
        "                  updated_at: p.updated_at}) AS items "
        "} "
        "RETURN total, items"
    )
    await _ensure_schema()
    driver = await _get_driver()
    result = await driver.execute_query(
        cypher,
        wiki=WIKI, prefix=prefix, offset=offset, limit=limit,
    )
    rec = result.records[0]
    total = rec["total"]
    items = [
        {
            "path": it["path"],
            "size": it["size"],
            "created_at": _iso(it["created_at"]),
            "updated_at": _iso(it["updated_at"]),
        }
        for it in rec["items"]
    ]

    return {
        "prefix": prefix or "",
        "total": total,
        "offset": offset if offset is not None else 0,
        "limit": limit if limit is not None else 10,
        "sort_by": sort_field,
        "order": order_norm,
        "items": items,
    }


async def search_memory(query: str, limit: int = 10) -> Any:
    """Full-text search across your agentic memory. Use this at the start of
    a task to check whether you already have relevant knowledge stored —
    about the user, the domain, or prior decisions — before asking or
    assuming.

    Args:
        query: Lucene-style full-text query.
        limit: Maximum number of results.
    """
    await _ensure_schema()
    driver = await _get_driver()
    result = await driver.execute_query(
        "CALL db.index.fulltext.queryNodes('page_fulltext', $q) "
        "YIELD node, score "
        "WHERE node.wiki = $wiki AND coalesce(node.deleted, false) = false "
        "RETURN node.path AS path, node.content AS content, score "
        "LIMIT $limit",
        q=query, wiki=WIKI, limit=limit,
    )
    hits = []
    for r in result.records:
        content = r["content"] or ""
        snippet = content[:240] + ("..." if len(content) > 240 else "")
        hits.append({"path": r["path"], "snippet": snippet, "score": r["score"]})
    return {"query": query, "results": hits}


async def rename_memory(
    old_path: str, new_path: str, overwrite: bool = False
) -> Any:
    """Atomically rename a memory; also rewrites `[[old_path]]` references
    to `[[new_path]]` in every memory that links to it. Use when you've
    learned a better name for something.

    By default, refuses if `new_path` already exists with non-empty content
    (a "real" page, not just an auto-created stub). Pass `overwrite=True`
    to clobber. Empty stubs created by wikilinks are always merged into.

    Args:
        old_path: Current memory page path.
        new_path: New memory page path.
        overwrite: If True, replace `new_path` even when it already has
            content. Defaults to False to prevent accidental data loss.
    """
    await _ensure_schema()
    old_path = _normalize(old_path)
    new_path = _normalize(new_path)
    if old_path == new_path:
        return {"ok": True, "path": new_path, "unchanged": True}

    driver = await _get_driver()
    if not overwrite:
        existing = await driver.execute_query(
            "MATCH (p:Page {wiki: $wiki, path: $path}) "
            "WHERE coalesce(p.deleted, false) = false "
            "  AND coalesce(p.content, '') <> '' "
            "RETURN p.path AS path",
            wiki=WIKI, path=new_path,
        )
        if existing.records:
            return {
                "error": True,
                "message": (
                    f"Refusing to rename: '{new_path}' already exists with "
                    "content. Pass overwrite=True to replace it."
                ),
                "old_path": old_path,
                "new_path": new_path,
            }

    # Move content+edges, rewrite referencing content, then delete old —
    # all in one statement so the structure and the text stay consistent.
    old_bare = old_path[:-3] if old_path.endswith(".md") else old_path
    new_bare = new_path[:-3] if new_path.endswith(".md") else new_path
    await driver.execute_query(
        "MATCH (old:Page {wiki: $wiki, path: $old_path}) "
        "MERGE (new:Page {wiki: $wiki, path: $new_path}) "
        "ON CREATE SET new.created_at = old.created_at "
        "SET new.content = old.content, new.deleted = false, "
        "    new.size = old.size, new.updated_at = datetime() "
        "WITH old, new "
        "OPTIONAL MATCH (old)-[:LINKS_TO]->(t) "
        "FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | "
        "  MERGE (new)-[:LINKS_TO]->(t)) "
        "WITH DISTINCT old, new "
        "OPTIONAL MATCH (src)-[:LINKS_TO]->(old) "
        "FOREACH (_ IN CASE WHEN src IS NULL THEN [] ELSE [1] END | "
        "  MERGE (src)-[:LINKS_TO]->(new)) "
        "FOREACH (_ IN CASE "
        "    WHEN src IS NULL OR NOT src.content CONTAINS '[[' THEN [] "
        "    ELSE [1] END | "
        "  SET src.content = "
        "    replace( "
        "      replace( "
        "        replace(src.content, '[[' + $old_bare + ']]', '[[' + $new_bare + ']]'), "
        "        '[[' + $old_path + ']]', '[[' + $new_path + ']]' "
        "      ), "
        "      '[[' + $old_bare + '|', '[[' + $new_bare + '|' "
        "    )) "
        "WITH DISTINCT old "
        "DETACH DELETE old",
        wiki=WIKI,
        old_path=old_path, new_path=new_path,
        old_bare=old_bare, new_bare=new_bare,
    )
    return {"ok": True, "old_path": old_path, "new_path": new_path}


async def delete_memory(path: str) -> Any:
    """Soft delete a memory. Use when a memory is obsolete or wrong; prefer
    rewriting over deleting when possible so history is preserved.

    Deletion is a flag flip, not a removal. Content, timestamps, and all
    `LINKS_TO` edges persist. `read`, `list`, and `search` all filter
    tombstoned pages out. New links from other pages to a tombstoned
    path still create edges (surfaced via `linked_to_deleted` in
    write/append responses); only writing at the tombstoned path itself
    resurrects it. Use `rename_memory` if you want references to move
    to a live page.

    Args:
        path: Memory page path to delete.
    """
    await _ensure_schema()
    path = _normalize(path)
    driver = await _get_driver()
    result = await driver.execute_query(
        "MATCH (p:Page {wiki: $wiki, path: $path}) "
        "SET p.deleted = true, p.updated_at = datetime() "
        "RETURN p.path AS path",
        wiki=WIKI, path=path,
    )
    if not result.records:
        return {"error": True, "message": f"Page not found: {path}"}
    return {"ok": True, "path": path}


def register(mcp) -> None:
    """Register memory tools on the FastMCP server."""
    mcp.tool()(read_memory)
    mcp.tool()(write_memory)
    mcp.tool()(append_memory)
    mcp.tool()(list_memories)
    mcp.tool()(search_memory)
    mcp.tool()(rename_memory)
    mcp.tool()(delete_memory)


def build_server() -> FastMCP:
    if not memory_enabled():
        missing = [
            v
            for v in ("NEO4J_MEMORY_URI", "NEO4J_MEMORY_USERNAME", "NEO4J_MEMORY_PASSWORD")
            if not os.getenv(v)
        ]
        raise RuntimeError("Missing required env vars: " + ", ".join(missing))

    mcp = FastMCP(
        name="neo4j-wiki-memory",
        instructions=(
            "Persistent agent memory backed by a Neo4j wiki. Pages are "
            "markdown, linked by [[wikilinks]]. Check search_memory / "
            "list_memories at the start of a task before assuming you have "
            "no prior context."
        ),
    )
    register(mcp)
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
            asyncio.run(_close_driver())
        except RuntimeError:
            # Event loop already closed (e.g. when FastMCP ran its own).
            pass


__all__ = ["register", "build_server", "main", "memory_enabled"]
