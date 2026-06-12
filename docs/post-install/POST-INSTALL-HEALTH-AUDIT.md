# Post-Install Health Audit — 6-item verification flow

Runnable by a non-tech-savvy user (GUI path first, CLI fallback second)
and paste-able into Claude for diagnosis via
[`CLAUDE-DIAGNOSTIC-PROMPT.md`](./CLAUDE-DIAGNOSTIC-PROMPT.md).

**When to run**: once after first install, once after every orchestrator
update, and any time something feels "off". Walk items 1–6 in order —
earlier failures cascade into later ones.

---

## 1. Containers running

**Checks**: Weaviate (vector DB), Ollama (embeddings), code-embed
service. Everything below depends on these.

**GUI**: launcher → **Services** tab → three green dots next to
Weaviate, Ollama, Code-embed. Grey/red = click row for last status.

**CLI**:

```bash
curl -s http://localhost:8081/v1/.well-known/ready && echo " weaviate OK"
curl -s http://localhost:11435/api/tags >/dev/null && echo "ollama OK"
curl -s http://localhost:11440/health >/dev/null && echo "code-embed OK"
```

Expect all three OK lines. If red:
[`CONTAINER-RECOVERY.md`](./CONTAINER-RECOVERY.md).

---

## 2. vct-hub reachable

**Checks**: the detached config service that resolves per-project KG /
codegraph / secrets for hooks and MCPs.

**GUI**: if the launcher is running, the hub is reachable.

**CLI**: `vct-hub --status` + `curl -s http://127.0.0.1:7700/api/v1/health`
should print `running (pid=…, port=7700)` and `{"status":"ok",...}`.
Use `/api/v1/health` — bare `/health` returns 401 (auth). Token at
`<vct_root>/hub.token` (`~/.vct/` Linux/macOS, `%USERPROFILE%\.vct\`
Windows; mode `0600`, regenerated every startup).

**If red**: usually self-heals via `session-start-ensure-hub.sh` next
session. Manual nudge: `vct-hub --start-if-not-running`. Port
contention → [`CONTAINER-RECOVERY.md`](./CONTAINER-RECOVERY.md).

---

## 3. MCPs registered

**Checks**: Claude Code can talk to the orchestrator's MCP servers
(semantic search, paper search).

**GUI**: in a Claude Code session, type `/mcp` — expect
`weaviate-kg ✓ Connected` and `search ✓ Connected`. Ollama is
infrastructure, not exposed as an MCP — its absence here is correct.

**CLI**: `claude mcp list` should show:

```
weaviate-kg    ✓ Connected
search         ✓ Connected
```

**If red**:

- `weaviate-kg ✗` → re-check item 1. If green, MCP venv missing — re-run
  `python install.py --update`.
- `search ✗` → usually a missing `GITHUB_TOKEN`; see
  `docs/CONFIGURATION.md`.
- Both missing → `~/.claude.json` not updated; re-run
  `python install.py --update`. Registration is additive — your other
  MCPs are preserved.

---

## 4. Weaviate collections present + correctly cased

**Checks**: per-project KG and shared cross-project KG both exist with
the casing the MCP expects. Casing mismatch is a SILENT failure —
search returns empty instead of erroring.

**GUI**: launcher → **Identity** tab — the two collection names should
match `/context` in the Claude session.

**CLI**:

```bash
curl -s http://localhost:8081/v1/schema \
  | python -c "import sys, json; d=json.load(sys.stdin); [print(c['class']) for c in sorted(d.get('classes', []), key=lambda x: x['class'])]"
```

Expect lines exactly matching `KG_COLLECTION` (e.g.
`MyProject_KnowledgeGraph`) + `SHARED_KG_COLLECTION` (default
`VibeCodedOrchestrator_KnowledgeGraph`). Every orchestrator collection
is `PascalCase` or `Upper_Snake_Case` — all-lowercase = created by a
non-orchestrator tool; the MCP won't find it. Migrate via
`python -m vco_lib.project_init migrate-collections --name <project>`
(prompts before any destructive step).

---

## 5. hybrid_search smoke test

**Checks**: end-to-end, a Claude session hits `hybrid_search` and gets
a non-empty result. Validates Weaviate + embedding model + per-project
wiring at once.

**GUI (recommended)**: in a Claude Code session, ask:

> Search the KG for "orchestrator architecture" and tell me what you found.

Expected: Claude returns 1+ result with a brief summary. "No results
found" or errors mentioning `KG_COLLECTION` / `WEAVIATE_URL` → items 3
or 4 are wrong.

**If red but items 1–4 green**: the per-project KG may be empty (no
`knowledge/**/*.md` yet). Try the same query against the shared KG
(name in the Identity tab) — it should always have content.

---

## 6. Code-graph indexed

**Checks**: the code-graph indexer has run at least once and results
are searchable.

**GUI**: launcher → **Identity** tab → **Code-graph status** should
read `indexed: N modules, M functions` with a recent timestamp.

**CLI**: `.claude/scripts/code-graph-query search "init" --limit 3` →
expect 1+ results with `path:` and `name:` fields.

**If red** (no results): bootstrap once with
`.claude/scripts/code-graph-analyze . --project "<name>"`. Incremental
updates happen via the `code-graph-incremental` PostToolUse hook on
every file edit.

---

## Extra items beyond the template's inline checklist

The template's minimal `Verifying Installation` block covers items 1
and 3 only. The audit above adds:

- **vct-hub `/api/v1/health`** (item 2). Full path matters; bare
  `/health` is auth-rejected.
- **Hub token** at `<vct_root>/hub.token`. Missing = hub never finished
  first startup.
- **Boot service** registered:
  - Linux: `systemctl --user status claude-mcp-containers.service`
  - macOS: `launchctl list | grep com.vibecodedtools.claude-mcp-containers`
  - Windows: `schtasks /Query /TN ClaudeMcpContainers`
- **Collection casing** (item 4). Silent failure mode.
- **`.vco-manifest.json`** at `<project>/.claude/.vco-manifest.json`.
  Missing = bundle never installed cleanly; rerun install-bundle once.

---

## Report template

Fill in and paste into Claude with the
[diagnostic prompt](./CLAUDE-DIAGNOSTIC-PROMPT.md) when you need help.

```markdown
## VibeCoded Orchestrator — post-install health report

- **OS / version**: <Linux 6.x / macOS 14 / Windows 11 …>
- **Install root**: <full path>
- **Project root**: <full path>
- **Orchestrator version**: <python install.py --version OR release tag>
- **Report date**: <YYYY-MM-DD>

| # | Item                                    | Status   | Notes |
|---|-----------------------------------------|----------|-------|
| 1 | Containers (Weaviate/Ollama/code-embed) | ✅/⚠️/❌ |       |
| 2 | vct-hub reachable                       | ✅/⚠️/❌ |       |
| 3 | MCPs registered (`claude mcp list`)     | ✅/⚠️/❌ |       |
| 4 | Weaviate collections + casing           | ✅/⚠️/❌ |       |
| 5 | hybrid_search smoke                     | ✅/⚠️/❌ |       |
| 6 | Code-graph indexed                      | ✅/⚠️/❌ |       |

Recovery docs already tried: [ ] CONTAINER [ ] UPDATE [ ] WINDOWS
UPDATE_DEFERRED.md entries (paste, or "absent"): ...
Anything else weird: ...
```

---

## Quick links

- [`CONTAINER-RECOVERY.md`](./CONTAINER-RECOVERY.md)
- [`UPDATE-RECOVERY.md`](./UPDATE-RECOVERY.md)
- [`WINDOWS-FIRST-RUN-CHECK.md`](./WINDOWS-FIRST-RUN-CHECK.md)
- [`CLAUDE-DIAGNOSTIC-PROMPT.md`](./CLAUDE-DIAGNOSTIC-PROMPT.md)
