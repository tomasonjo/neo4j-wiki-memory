"""
Persistent agent memory backed by Neo4j.

Models a markdown-style wiki where each page is a `Page` node and `[[wikilinks]]`
become `LINKS_TO` relationships. All pages are scoped to a single `wiki`
namespace (configured via NEO4J_MEMORY_WIKI, default "default").

Env vars (all required to enable these tools):
  NEO4J_MEMORY_URI
  NEO4J_MEMORY_USERNAME
  NEO4J_MEMORY_PASSWORD
  NEO4J_MEMORY_WIKI      - optional, defaults to "default"
"""

from __future__ import annotations

import os
import re
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncDriver

WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

_driver: AsyncDriver | None = None
_schema_ready = False
WIKI = os.getenv("NEO4J_MEMORY_WIKI", "default")


def _normalize(target: str) -> str:
    target = target.strip()
    if not target.endswith(".md"):
        target += ".md"
    return target


def _extract_links(content: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in WIKILINK_RE.finditer(content or ""):
        t = _normalize(m.group(1))
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


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
    async with driver.session() as s:
        await s.run(
            "CREATE CONSTRAINT page_wiki_path IF NOT EXISTS "
            "FOR (p:Page) REQUIRE (p.wiki, p.path) IS UNIQUE"
        )
        await s.run(
            "CREATE FULLTEXT INDEX page_fulltext IF NOT EXISTS "
            "FOR (p:Page) ON EACH [p.path, p.content]"
        )
    _schema_ready = True


def memory_enabled() -> bool:
    return all(
        os.getenv(v)
        for v in ("NEO4J_MEMORY_URI", "NEO4J_MEMORY_USERNAME", "NEO4J_MEMORY_PASSWORD")
    )


# --- tool implementations ------------------------------------------------


async def read_memory(path: str) -> Any:
    """Read a stored memory page from your agentic memory. Use this to recall
    what you've previously learned and saved about a topic, person, or the
    user before answering. Memory persists across sessions — always check
    here for relevant context.

    Args:
        path: Memory page path, e.g. "user/profile.md".
    """
    await _ensure_schema()
    path = _normalize(path)
    driver = await _get_driver()
    async with driver.session() as s:
        result = await s.run(
            "MATCH (p:Page {wiki: $wiki, path: $path}) "
            "WHERE coalesce(p.deleted, false) = false "
            "RETURN p.content AS content",
            wiki=WIKI,
            path=path,
        )
        record = await result.single()
    if record is None:
        return {"error": True, "message": f"Page not found: {path}"}
    return {"path": path, "content": record["content"] or ""}


async def write_memory(path: str, content: str) -> Any:
    """Save or overwrite a memory in your agentic memory. Use this whenever
    you want to remember something for later: facts about the user
    (preferences, goals, background, working style), insights from
    conversations, decisions made, patterns you've noticed, or any concept
    worth recalling in future sessions. Parses `[[wikilinks]]` from content
    and links to those memories, auto-creating empty stubs as needed.

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
    graph edge you can traverse later via `find_memory_backlinks`.
    Prefer refining an existing page over creating near-duplicates.

    Args:
        path: Memory page path, e.g. "user/profile.md".
        content: Full markdown content of the memory.
    """
    await _ensure_schema()
    path = _normalize(path)
    links = _extract_links(content)
    driver = await _get_driver()
    async with driver.session() as s:
        await s.execute_write(_write_tx, path, content, links)
    return {"ok": True, "path": path, "links": links}


async def _write_tx(tx, path: str, content: str, links: list[str]) -> None:
    await tx.run(
        "MERGE (p:Page {wiki: $wiki, path: $path}) "
        "ON CREATE SET p.created_at = datetime() "
        "SET p.content = $content, p.deleted = false, "
        "    p.size = size($content), p.updated_at = datetime()",
        wiki=WIKI,
        path=path,
        content=content,
    )
    # Drop existing outgoing links and rebuild from current content.
    await tx.run(
        "MATCH (p:Page {wiki: $wiki, path: $path})-[r:LINKS_TO]->() DELETE r",
        wiki=WIKI,
        path=path,
    )
    if links:
        await tx.run(
            "MATCH (p:Page {wiki: $wiki, path: $path}) "
            "UNWIND $links AS target "
            "MERGE (t:Page {wiki: $wiki, path: target}) "
            "ON CREATE SET t.content = '', t.deleted = false, "
            "              t.size = 0, t.created_at = datetime(), "
            "              t.updated_at = datetime() "
            "MERGE (p)-[:LINKS_TO]->(t)",
            wiki=WIKI,
            path=path,
            links=links,
        )


async def append_memory(path: str, content: str) -> Any:
    """Append to an existing memory without rewriting it. Use for running
    logs (`log.md`), timelines on an entity, or accumulating observations
    about the user over time. Adds new links for any wikilinks in the
    appended text.

    Args:
        path: Memory page path, e.g. "log.md".
        content: Markdown to append. A newline is inserted before it if the
            existing memory does not already end with one.
    """
    await _ensure_schema()
    path = _normalize(path)
    links = _extract_links(content)
    driver = await _get_driver()
    async with driver.session() as s:
        result = await s.run(
            "MERGE (p:Page {wiki: $wiki, path: $path}) "
            "ON CREATE SET p.content = '', p.deleted = false, "
            "              p.created_at = datetime() "
            "SET p.deleted = false, "
            "    p.content = CASE "
            "      WHEN coalesce(p.content, '') = '' THEN $content "
            "      WHEN right(p.content, 1) = '\n' THEN p.content + $content "
            "      ELSE p.content + '\n' + $content END, "
            "    p.size = size(CASE "
            "      WHEN coalesce(p.content, '') = '' THEN $content "
            "      WHEN right(p.content, 1) = '\n' THEN p.content + $content "
            "      ELSE p.content + '\n' + $content END), "
            "    p.updated_at = datetime() "
            "RETURN p.path AS path",
            wiki=WIKI,
            path=path,
            content=content,
        )
        await result.consume()
        if links:
            await s.run(
                "MATCH (p:Page {wiki: $wiki, path: $path}) "
                "UNWIND $links AS target "
                "MERGE (t:Page {wiki: $wiki, path: target}) "
                "ON CREATE SET t.content = '', t.deleted = false, "
                "              t.size = 0, t.created_at = datetime(), "
                "              t.updated_at = datetime() "
                "MERGE (p)-[:LINKS_TO]->(t)",
                wiki=WIKI,
                path=path,
                links=links,
            )
    return {"ok": True, "path": path, "added_links": links}


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
    # Validate only what the caller actually supplied; defaults are
    # applied below (and again by Cypher's coalesce as a safety net).
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
    cypher = (
        "MATCH (p:Page {wiki: $wiki}) "
        "WHERE coalesce(p.deleted, false) = false "
        "  AND p.path STARTS WITH coalesce($prefix, '') "
        "WITH count(p) AS total, collect(p) AS pages "
        "UNWIND pages AS p "
        "WITH total, p "
        f"ORDER BY p.{sort_field} {direction}, p.path ASC "
        "SKIP coalesce($offset, 0) LIMIT coalesce($limit, 10) "
        "RETURN total, p.path AS path, p.size AS size, "
        "       p.created_at AS created_at, "
        "       p.updated_at AS updated_at"
    )
    await _ensure_schema()
    driver = await _get_driver()
    async with driver.session() as s:
        result = await s.run(
            cypher,
            {"wiki": WIKI, "prefix": prefix, "offset": offset, "limit": limit},
        )
        items: list[dict[str, Any]] = []
        total = 0
        async for r in result:
            total = r["total"]
            items.append(
                {
                    "path": r["path"],
                    "size": r["size"],
                    "created_at": _iso(r["created_at"]),
                    "updated_at": _iso(r["updated_at"]),
                }
            )
        if not items:
            # No matches in this page — still need the total so callers
            # can tell "empty page" from "filter matched nothing".
            tot_result = await s.run(
                "MATCH (p:Page {wiki: $wiki}) "
                "WHERE coalesce(p.deleted, false) = false "
                "  AND p.path STARTS WITH coalesce($prefix, '') "
                "RETURN count(p) AS total",
                {"wiki": WIKI, "prefix": prefix},
            )
            rec = await tot_result.single()
            total = rec["total"] if rec else 0

    return {
        "prefix": prefix or "",
        "total": total,
        "offset": offset if offset is not None else 0,
        "limit": limit if limit is not None else 10,
        "sort_by": sort_field,
        "order": order_norm,
        "items": items,
    }


def _iso(value: Any) -> Any:
    """Render a Neo4j DateTime to an ISO-8601 string; pass None through."""
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


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
    async with driver.session() as s:
        # NB: pass parameters as a dict — using `query=...` as a kwarg
        # collides with AsyncSession.run's first positional `query` arg.
        result = await s.run(
            "CALL db.index.fulltext.queryNodes('page_fulltext', $q) "
            "YIELD node, score "
            "WHERE node.wiki = $wiki AND coalesce(node.deleted, false) = false "
            "RETURN node.path AS path, node.content AS content, score "
            "LIMIT $limit",
            {"q": query, "wiki": WIKI, "limit": limit},
        )
        hits = []
        async for r in result:
            content = r["content"] or ""
            snippet = content[:240] + ("..." if len(content) > 240 else "")
            hits.append({"path": r["path"], "snippet": snippet, "score": r["score"]})
    return {"query": query, "results": hits}


async def find_memory_backlinks(path: str) -> Any:
    """Return all memories that link to this one. Use to find where an
    entity or concept has come up across your agentic memory.

    Args:
        path: Memory page path to find backlinks for.
    """
    await _ensure_schema()
    path = _normalize(path)
    driver = await _get_driver()
    async with driver.session() as s:
        result = await s.run(
            "MATCH (other:Page {wiki: $wiki})-[:LINKS_TO]->(p:Page {wiki: $wiki, path: $path}) "
            "WHERE coalesce(other.deleted, false) = false "
            "RETURN other.path AS path ORDER BY other.path",
            wiki=WIKI,
            path=path,
        )
        backlinks = [r["path"] async for r in result]
    return {"path": path, "backlinks": backlinks}


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
    async with driver.session() as s:
        if not overwrite:
            existing = await s.run(
                "MATCH (p:Page {wiki: $wiki, path: $path}) "
                "WHERE coalesce(p.deleted, false) = false "
                "  AND coalesce(p.content, '') <> '' "
                "RETURN p.path AS path",
                {"wiki": WIKI, "path": new_path},
            )
            collision = await existing.single()
            if collision is not None:
                return {
                    "error": True,
                    "message": (
                        f"Refusing to rename: '{new_path}' already exists with "
                        "content. Pass overwrite=True to replace it."
                    ),
                    "old_path": old_path,
                    "new_path": new_path,
                }
        await s.execute_write(_rename_tx, old_path, new_path)
    return {"ok": True, "old_path": old_path, "new_path": new_path}


async def _rename_tx(tx, old_path: str, new_path: str) -> None:
    # Move content+links onto the new path. If new already exists as a stub,
    # overwrite it; otherwise create. Then delete the old node.
    await tx.run(
        "MATCH (old:Page {wiki: $wiki, path: $old_path}) "
        "MERGE (new:Page {wiki: $wiki, path: $new_path}) "
        "ON CREATE SET new.created_at = old.created_at "
        "SET new.content = old.content, new.deleted = false, "
        "    new.size = old.size, new.updated_at = datetime() "
        "WITH old, new "
        "OPTIONAL MATCH (old)-[r:LINKS_TO]->(t) "
        "FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | "
        "  MERGE (new)-[:LINKS_TO]->(t)) "
        "WITH old, new "
        "OPTIONAL MATCH (src)-[r2:LINKS_TO]->(old) "
        "FOREACH (_ IN CASE WHEN src IS NULL THEN [] ELSE [1] END | "
        "  MERGE (src)-[:LINKS_TO]->(new)) "
        "DETACH DELETE old",
        wiki=WIKI,
        old_path=old_path,
        new_path=new_path,
    )
    # Rewrite [[old]] / [[old|alias]] in referencing pages' content. Match the
    # bare name without .md and the .md form for safety.
    old_bare = old_path[:-3] if old_path.endswith(".md") else old_path
    new_bare = new_path[:-3] if new_path.endswith(".md") else new_path
    await tx.run(
        "MATCH (src:Page {wiki: $wiki})-[:LINKS_TO]->(:Page {wiki: $wiki, path: $new_path}) "
        "WHERE src.content CONTAINS '[[' "
        "SET src.content = "
        "  replace( "
        "    replace( "
        "      replace(src.content, '[[' + $old_bare + ']]', '[[' + $new_bare + ']]'), "
        "      '[[' + $old_path + ']]', '[[' + $new_path + ']]' "
        "    ), "
        "    '[[' + $old_bare + '|', '[[' + $new_bare + '|' "
        "  )",
        wiki=WIKI,
        new_path=new_path,
        old_path=old_path,
        old_bare=old_bare,
        new_bare=new_bare,
    )


async def delete_memory(path: str) -> Any:
    """Soft delete a memory. Use when a memory is obsolete or wrong; prefer
    rewriting over deleting when possible so history is preserved.

    Args:
        path: Memory page path to delete.
    """
    await _ensure_schema()
    path = _normalize(path)
    driver = await _get_driver()
    async with driver.session() as s:
        result = await s.run(
            "MATCH (p:Page {wiki: $wiki, path: $path}) "
            "SET p.deleted = true, p.updated_at = datetime() "
            "RETURN p.path AS path",
            wiki=WIKI,
            path=path,
        )
        record = await result.single()
    if record is None:
        return {"error": True, "message": f"Page not found: {path}"}
    return {"ok": True, "path": path}


def register(mcp) -> None:
    """Register memory tools on the FastMCP server."""
    mcp.tool()(read_memory)
    mcp.tool()(write_memory)
    mcp.tool()(append_memory)
    mcp.tool()(list_memories)
    mcp.tool()(search_memory)
    mcp.tool()(find_memory_backlinks)
    mcp.tool()(rename_memory)
    mcp.tool()(delete_memory)
