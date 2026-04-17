# neo4j-wiki-memory

Persistent agent memory backed by **Neo4j**, exposed as a standalone
[FastMCP](https://github.com/jlowin/fastmcp) server.

Each memory is a markdown page. `[[wikilinks]]` in the content become
`LINKS_TO` relationships in the graph, so your memory forms a browseable,
backlink-aware knowledge graph instead of a flat log.

## Tools

| Tool | Purpose |
|------|---------|
| `read_memory(path, include_backlinks?)` | Recall a page. Pass `include_backlinks=True` to also get every page that links to it. |
| `write_memory(path, content)` | Save or overwrite a page. Parses `[[wikilinks]]` and auto-creates stub targets. |
| `append_memory(path, content)` | Append to an existing page (good for logs, timelines). |
| `list_memories(prefix?, limit?, offset?, sort_by?, order?)` | Browse pages with pagination and sorting. |
| `search_memory(query, limit?)` | Lucene-style full-text search over paths and content. |
| `rename_memory(old_path, new_path, overwrite?)` | Rename a page and rewrite `[[wikilinks]]` pointing at it. |
| `delete_memory(path)` | Soft delete (sets `deleted = true`). |

## Environment variables

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `NEO4J_MEMORY_URI` | ✅ | — | Bolt URI, e.g. `neo4j+s://xxxxx.databases.neo4j.io` |
| `NEO4J_MEMORY_USERNAME` | ✅ | — | Neo4j username |
| `NEO4J_MEMORY_PASSWORD` | ✅ | — | Neo4j password |
| `NEO4J_MEMORY_WIKI` | ❌ | `default` | Namespace tag. Use different values to isolate memory sets in one database. |

On first use the server creates a unique constraint on `(:Page {wiki, path})`
and a fulltext index `page_fulltext` on `(path, content)` using the
`standard-folding` analyzer (case- and accent-insensitive, so `café`
matches `cafe`). Installs from before v0.2.0 used the default analyzer;
the server detects the mismatch on startup and recreates the index in
place.

## Wikilink parsing

- `[[target]]` and `[[target|alias]]` create outbound edges. Aliases
  are rendered only — the graph edge keys on `target`.
- Parsing is **context-aware**: `[[...]]` inside fenced code blocks
  (```` ``` ```` and `~~~`) and inline code spans (`` ` `` or `` `` ``)
  is ignored.
- Backslash-escape suppresses parsing: `\[\[foo\]\]` stays literal and
  creates no edge. Use this whenever documenting wikilink syntax in
  prose so examples don't stub-spam the graph.
- Links do not span newlines. Empty `[[]]`, nested `[[[x]]]`, and
  trailing-slash targets like `[[foo/]]` are rejected. Paths are
  case-sensitive (`Foo.md` ≠ `foo.md`).
- `write_memory` replaces a page's outbound edge set. `append_memory`
  only parses the appended chunk and returns the delta as `added_links`.
- Linking to a tombstoned (soft-deleted) target creates the edge but
  does not resurrect the page. The response surfaces those paths under
  `linked_to_deleted`; only writing at the tombstoned path itself flips
  the `deleted` flag back.

## Install & run

### With `uv` (recommended)

```bash
git clone https://github.com/tomasonjo/neo4j-wiki-memory.git
cd neo4j-wiki-memory
uv sync
uv run neo4j-wiki-memory
```

### With `pip`

```bash
git clone https://github.com/tomasonjo/neo4j-wiki-memory.git
cd neo4j-wiki-memory
pip install -e .
neo4j-wiki-memory
```

### Transports

Defaults to stdio (what MCP clients expect). For HTTP or SSE:

```bash
neo4j-wiki-memory --transport http --host 127.0.0.1 --port 8000
neo4j-wiki-memory --transport sse  --host 127.0.0.1 --port 8000
```

## Use with Claude (Claude Code / Claude Desktop)

Add the server to your MCP config. Replace `/absolute/path/to/neo4j-wiki-memory`
with the path to your clone, and fill in your Neo4j credentials.

```json
{
  "mcpServers": {
    "neo4j-wiki-memory": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/neo4j-wiki-memory",
        "run",
        "neo4j-wiki-memory"
      ],
      "env": {
        "NEO4J_MEMORY_URI": "neo4j+s://xxxxx.databases.neo4j.io",
        "NEO4J_MEMORY_USERNAME": "neo4j",
        "NEO4J_MEMORY_PASSWORD": "your-password",
        "NEO4J_MEMORY_WIKI": "default"
      }
    }
  }
}
```

**Claude Code**: drop this into `~/.claude.json` under `mcpServers`, or register
it via CLI:

```bash
claude mcp add-json neo4j-wiki-memory '{
  "command": "uv",
  "args": ["--directory", "/absolute/path/to/neo4j-wiki-memory", "run", "neo4j-wiki-memory"],
  "env": {
    "NEO4J_MEMORY_URI": "neo4j+s://xxxxx.databases.neo4j.io",
    "NEO4J_MEMORY_USERNAME": "neo4j",
    "NEO4J_MEMORY_PASSWORD": "your-password"
  }
}'
```

**Claude Desktop**: put the same block in `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS, `%APPDATA%\Claude\` on Windows).

## Recommended page layout

Organise memories by topic, not by conversation, so they can be recalled
independently of when they were learned:

```
user/profile.md              # who they are, role, responsibilities
user/preferences.md          # tooling, style, do / don't

databases/<dbid>.md          # one page per database — schema, gotchas, queries
databases/<dbid>/<topic>.md  # optional deep-dives

agents/<agent_id>.md         # one page per agent — purpose, tools, failure modes
agents/<agent_id>/<topic>.md

entities/<name>.md           # people, orgs, services, repos
concepts/<name>.md           # domain ideas worth knowing
learnings/<topic>.md         # cross-cutting lessons
log.md                       # scratch / chronological notes
```

Cross-link liberally with `[[wikilinks]]` — every edge is something you can
traverse later via `read_memory(path, include_backlinks=True)`. Prefer
refining an existing page over creating near-duplicates.

## License

Apache-2.0. See [LICENSE](LICENSE).
