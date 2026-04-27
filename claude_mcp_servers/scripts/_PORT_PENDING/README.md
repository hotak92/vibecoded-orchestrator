# Pending CLI Script Port (manual apply needed)

## Why this directory exists

The score-driven auto-tier KG retrieval port (mirrors Claude orch commits
`595f866` + `ae26928`) needs to update two CLI wrappers under
`.claude/scripts/`. The Claude Code sandbox in this session denies all writes
under `vibecoded-orchestrator/.claude/`, so the patched files are staged here
for the user to copy manually.

## Apply

```bash
cp claude_mcp_servers/scripts/_PORT_PENDING/search_knowledge.py .claude/scripts/search_knowledge.py
cp claude_mcp_servers/scripts/_PORT_PENDING/query_code_graph.py .claude/scripts/query_code_graph.py
chmod +x .claude/scripts/search_knowledge.py .claude/scripts/query_code_graph.py
rm -rf claude_mcp_servers/scripts/_PORT_PENDING
```

## What changed

- **`search_knowledge.py`**: New `--detail {auto,titles,summary,descriptions,full}` flag.
  `--files-only` and `--content` are kept as legacy aliases for `--detail titles`
  and `--detail full` respectively. The `auto` mode uses
  `_get_result_verbosity_by_score` + `_format_result_by_tier` from
  `weaviate_mcp/server.py` so output mirrors `hybrid_search(detail="auto")`.

- **`query_code_graph.py`**: New `--detail {auto,titles,full}` flag on the
  `search` subcommand. `auto` mirrors the MCP `search_code_graph` heuristic
  (top-4 full, rest as refs).

Both scripts already carry the AGPL header. Both compile cleanly under Python
3.12 (`python3 -m py_compile <file>`).

## Diff vs. current vibecoded versions

The patched files are byte-for-byte identical to Claude orch's
`.claude/scripts/{search_knowledge,query_code_graph}.py` from commit `ae26928`,
plus the two-line AGPL/Copyright header at the top.

The vibecoded baseline diff vs. Claude orch was just that header (verified pre-port).
