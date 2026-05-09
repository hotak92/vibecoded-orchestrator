# Group B doc audit — features/ (factual + tone sweep)

Date: 2026-05-09
Branch: `audit/group-b-features`
Reviewer: Group B

## Scope

`docs/features/INDEX.md` + `docs/features/01-launcher.md` … `07-architecture.md`. Verified every concrete claim against the v0.1.0 OSS bundle (worktree off `origin/main`, base `af274b8`).

## Mechanical fixes applied

### `docs/features/INDEX.md`
- "four MCP servers" → "five MCP servers" (Playwright is the 5th — registered + pre-cached at install).
- "19 free agents, 28 skills, and 20 hooks" → "29 free agents, 28 skills, and 23 hooks" (file counts verified).
- Glossary "MAO" entry rewritten to describe the DB host value + tier name without claiming the agent stack ships separately.
- Updated `02-` and `03-` row descriptions to match new counts.

### `docs/features/01-launcher.md`
- Removed the "Legacy Project Commands (`commands/projects.rs`)" section — `commands/projects.rs` doesn't exist in v0.1.0; only `projects_v2.rs` is wired in `lib.rs`.
- "Five migrations" → "Eight migrations" (verified `launcher/src-tauri/src/db/migrations/`: 001 through 008).
- "Seventeen commands" in `commands/installer.rs` → "Twenty-one commands" (verified count via `grep commands::installer:: lib.rs`). Added `detect_existing_install_root`, `check_install_health`, `read_install_log`, `inspect_project_leftovers` to the enumeration.
- Hub API section: documented the Bearer-token auth gate (`~/.vct/hub.token`, regenerated each startup, mode 0o600). Previous text claimed routes were "localhost-only with permissive CORS" with no auth — actually every `/api/v1/*` request requires `Authorization: Bearer <token>`.
- CORS Wildcard description: clarified the auth boundary is the Bearer token, not the origin header.
- Hub API route enumeration: corrected `modules_api.rs` 8 → 9 routes (added `/projects/by-path`); corrected `cli_api.rs` 10 → 14 routes (added `kg/collections`, `kg/search`, `codegraph/collections`, `codegraph/search`).
- "VCT_VALIDATE_TIER_URL Override" — fixed line reference: `commands/licensing.rs:21` → `commands/licensing.rs::DEFAULT_VALIDATE_TIER_URL` (actual line is 37).
- Machine ID Binding: "SHA-256 of the 6-byte MAC address" → clarified that `uuid.getnode().to_bytes(8, "big")` zero-pads the 6-byte MAC to 8 bytes before hashing.

### `docs/features/02-mcps-and-agents.md`
- Header rewritten: "Four MCP servers" → "Five MCP servers" (Weaviate-KG, Ollama, Search, code-embed, Playwright). Added Playwright section before Infrastructure Scripts.
- Removed `natural_language_code_query` MCP tool documentation — the tool is not defined in `weaviate_mcp/server.py` (verified by `grep '@mcp.tool'`). Code-doc gap surfaced as deletion.
- Ollama `chat` default model: `qwen3.5:9b` → `qwen3.5:0.8b` (verified at `ollama_mcp/server.py:448`). Reworded the "Supported models" enumeration to match `TEXT_MODEL_TIERS` actually present in source rather than the speculative `qwen3-coder` / `devstral` / `olmo-3` list.
- `read_document` default model: `qwen3.5:9b` → "default per Ollama MCP" (signature in source is the source of truth).
- Search MCP: `~/.vct-secrets/search-mcp-wrapper.sh` → `claude_mcp_servers/search_mcp/wrapper.sh` (the file actually shipped). Documented the v0.1.7 two-stage env-first / hub-resolver flow and the removal of the legacy file fallback.

### `docs/features/03-agents-skills-hooks.md`
- Header: "29 bundled agents, 28 skills, and 20 hooks" → "29 bundled agents, 28 skills, and 23 hooks (22 wired + 1 available-not-wired)".
- "The 19 agents below" → "The 29 agents below" (matches header — actual count from `templates/agents/free/`).
- "Three agents reference orchestrator-tools" → "Seven agents" (verified: `coder`, `tester`, `planner`, `expert-coder`, `project-architect`, `project-coordinator`, `ai-agentic-architect`).
- Hooks section intro: 20 → 23, added v2.1.x stdin-JSON input contract callout.
- Added 3 missing hook entries: `kg-update-nudge.sh`, `verify-container-ports.sh`, `pre-vercel-token-guard.sh`.
- Updated `cost-tracker.sh` description to include the `auth_mode` field (subscription vs api).
- `pre-edit-context-inject.sh` matcher: "Edit(*)" → "Edit" (matches actual settings.json `"matcher": "Edit"`).
- `pre-tool-use.sh` matcher: "(all tools, blocking)" → "PreToolUse `*` (all tools, blocking)" for clarity.
- `post-file-edit.sh` and `post-tool-security.sh` matchers: `Edit(*)|Write(*)` → `Edit|Write` (matches actual settings.json `"matcher": "Edit|Write"`).
- Removed the bogus claim that `orchestrator-tools` is a "Pro-tier MAO component" (rephrased as just "paid module"). The doc shouldn't claim membership in a stack that isn't visible in this repo.
- `doc-extractor` agent: rewrote the "read-only enforced by `validate-readonly.sh`" claim — the agent declares an agent-scoped hook pointing at `.claude/scripts/validate-readonly.sh` but that script doesn't ship in v0.1.0; flagged as code-doc gap.

### `docs/features/04-knowledge-and-code-graph.md`
- "50 seed nodes total — 34 concepts / 5 models / 9 tools / 2 patterns" → "64 seed nodes total — 48 / 6 / 9 / 1" (verified `ls knowledge/<sub>/ | wc -l`).
- Internal contradiction fixed: header was "5 models" but section "6 model nodes" — both now consistently 6.
- "1 pattern node" — corrected from claimed "2 patterns" — only `prompt-engineering-2026.md` exists in `patterns/`.
- "ACTIVE_EMBEDDING env var" — rewrote to match actual MCP source: only `qwen3` / `openai` / `codesage` are explicit branches; legacy values fall through the negation branch to `ollama_embed` / `ollama_code_embed`. Documented that `arctic` (written by `--low-resource` profile) lands in this legacy branch.
- "Cross-OS install support" footnote: same clarification.
- `documents/` ingestion: removed the claim of auto-trigger via `PostToolUse Write(documents/**)` hook — no such hook is wired in `.claude/settings.json`. Documented as manual-only via `process_documents.py`.

### `docs/features/05-install-and-secrets.md`
- `install.sh` Python interpreter probe: added `python3.13` to the probe order (verified in source).
- `install.ps1`: removed `-WithMaoAgents` from the "supports all flags" enumeration; added a callout that the switch is present but forwards to a flag (`--with-mao-agents`) that `install.py` doesn't define + a directory (`templates/agents/mao/`) that doesn't exist in OSS.
- `ACTIVE_EMBEDDING env var`: rewrote to reflect actual recognized values + the `arctic` legacy-branch behavior.
- `.env.example` section heading and content rewritten — the repo does not ship a top-level `.env.example`. The actual artefact is the `.env` generated by `install.py::_write_env_config` at install time. The launcher subtree carries its own `launcher/.env.example` for the SvelteKit / Supabase auth client.
- "Search MCP wrapper (`~/.vct-secrets/search-mcp-wrapper.sh`)" → `claude_mcp_servers/search_mcp/wrapper.sh` + correct two-stage resolution narrative.

### `docs/features/06-license-and-commercial.md`
- "Env scrubbing in hooks": "All 20 project hooks" → "All 23 project hooks".
- "Machine ID hash": clarified the 6→8 byte pad / `to_bytes(8, "big")` mechanic, added cross-reference to the Rust mirror.

### `docs/features/07-architecture.md`
- "Python job: pytest (73+ trust-critical tests)" → "~100 trust-critical tests". Actual count at v0.1.0: 99 `def test_*` declarations across 37 test files.

## Tone sweep

Searched for marketing voice: `beautifully|perfectly|seamless|comprehensive|powerful|best-in-class|cutting-edge|world-class|effortless|blazing|robust|elegant`. No matches that warranted rewrites — the prose is already terse. The remaining "gracefully", "best-effort", and "effort" hits are technical (frontmatter field names, Tauri-event flow descriptions, fail-open behavior).

## Roadmap promises

Reviewed all "future / planned / coming soon / TBD / will" hits. All flagged items either:
- Cite an explicit reason (kg-infer wrapper-only, Cloudflare planned but not deployed, `coming_soon` module field is a documented manifest schema), OR
- Are technical descriptions of behavior over time (fail-open, future audit log appends, etc.).

No speculative roadmap promises that needed deletion.

## Code-doc gaps surfaced

Where the doc described feature the code doesn't have OR described outdated behavior:

```
GAP: docs/features/02-mcps-and-agents.md (pre-edit) had a `natural_language_code_query` tool section.
     Code at claude_mcp_servers/weaviate_mcp/server.py shows no such @mcp.tool definition (only
     hybrid_search, semantic_graph_search, store_knowledge_node, search_code_graph, query_code_structure).
     Action: doc updated by deletion. If the tool is intended, it needs to be implemented in the MCP server.

GAP: docs/features/02-mcps-and-agents.md claimed Ollama `chat` default model is `qwen3.5:9b`.
     Code at claude_mcp_servers/ollama_mcp/server.py:448 shows `model: str = "qwen3.5:0.8b"`.
     Action: doc updated. Worth checking whether the small model is genuinely the desired default
     (vs a leftover from a hardware-tuning PR) — verify with user.

GAP: docs/features/01-launcher.md described `commands/projects.rs` (legacy) with five v1 commands
     (`create_project`, `get_projects`, etc.). The file does not exist; only `projects_v2.rs` is wired.
     `~/.vct/projects.json` is referenced once in `lib.rs:385` and `paths.rs:3` but no Tauri command
     reads/writes it.
     Action: doc updated by deletion. Verify with user that the legacy v1 path is fully retired
     before any external consumer might still expect it.

GAP: docs/features/01-launcher.md Hub API section described "all routes are localhost-only with
     permissive CORS (intentional)" with no mention of auth. Code at hub/server.rs:86 shows
     `axum::middleware::from_fn(auth::require_auth)` wrapping every route, plus a 32-byte CSPRNG
     bearer token at `~/.vct/hub.token`.
     Action: doc updated to describe the Bearer-token gate.

GAP: docs/features/01-launcher.md said "Seventeen commands wired in lib.rs" for installer.rs.
     Actual is 21 (verified by grep). Missing from doc: `detect_existing_install_root`,
     `check_install_health`, `read_install_log`, `inspect_project_leftovers`.
     Action: doc updated.

GAP: docs/features/01-launcher.md said "Five migrations". Actual is 8 (006, 007, 008 added).
     Action: doc updated with all eight migration names.

GAP: docs/features/01-launcher.md installer Tauri commands list omits the `inspect_project_leftovers`
     command but lib.rs registers it. Mentioned in the GAP above; doc is updated.

GAP: docs/features/01-launcher.md said `commands/licensing.rs:21` for the hard-coded validate-tier URL.
     Actual location is the `DEFAULT_VALIDATE_TIER_URL` constant at line 37.
     Action: doc updated to reference the symbol name rather than a fragile line number.

GAP: docs/features/03-agents-skills-hooks.md said "Three agents (coder, tester, planner) reference
     orchestrator-tools". Actual is seven: also `expert-coder`, `project-architect`,
     `project-coordinator`, `ai-agentic-architect`.
     Action: doc updated.

GAP: docs/features/03-agents-skills-hooks.md `doc-extractor` agent was described as "Read-only at
     runtime, enforced by the `validate-readonly.sh` PreToolUse hook". The agent's frontmatter does
     declare such a hook (pointing at `./.claude/scripts/validate-readonly.sh`), but the script
     doesn't exist in v0.1.0. So read-only enforcement is convention-only.
     Action: doc updated to flag the gap. Either ship the script or remove the hook entry from
     the agent template.

GAP: docs/features/03-agents-skills-hooks.md missed three hook scripts (`kg-update-nudge.sh`,
     `verify-container-ports.sh`, `pre-vercel-token-guard.sh`) — all wired in `.claude/settings.json`.
     Action: doc updated with three new entries.

GAP: docs/features/04-knowledge-and-code-graph.md claimed "50 seed nodes total — 34 / 5 / 9 / 2".
     Actual: 64 — 48 / 6 / 9 / 1.
     Action: doc updated.

GAP: docs/features/04-knowledge-and-code-graph.md had an internal contradiction (header "5 model
     nodes" vs section "6 model nodes").
     Action: both now consistently 6.

GAP: docs/features/04-knowledge-and-code-graph.md claimed `documents/` files trigger a
     `PostToolUse Write(documents/**)` auto-processing hook. No such hook is wired in
     `.claude/settings.json`.
     Action: doc rewritten to describe `process_documents.py` as a manual script.

GAP: docs/features/04-knowledge-and-code-graph.md `ACTIVE_EMBEDDING` description claimed `"ollama"`
     is a recognized branch. Actual: server.py only branches on `"qwen3"` / `"openai"` / `"codesage"`;
     all other values fall through the negation branch. The `--low-resource` install profile writes
     `arctic` which therefore reaches the legacy slot, not via an `arctic` branch.
     Action: doc rewritten. Action for the codebase: either add an explicit `arctic`/`legacy` branch
     in server.py or document the negation behavior in code comments — verify with user.

GAP: docs/features/05-install-and-secrets.md `install.ps1` flag enumeration listed
     `-WithMaoAgents` as a supported flag. The switch is in install.ps1 source but install.py
     doesn't define a `--with-mao-agents` flag and no `templates/agents/mao/` exists in OSS.
     Action: doc updated to flag the orphan switch. Action for code: either remove the PS1 switch,
     or land the matching install.py flag + agents directory — verify with user.

GAP: docs/features/05-install-and-secrets.md described an "annotated `.env.example` template" at
     the repo root. No such file exists; only `launcher/.env.example` (for the SvelteKit / Supabase
     client). The orchestrator's `.env` is generated by `install.py::_write_env_config`.
     Action: doc rewritten.

GAP: docs/features/05-install-and-secrets.md described the search MCP wrapper as living at
     `~/.vct-secrets/search-mcp-wrapper.sh`. Actual: `claude_mcp_servers/search_mcp/wrapper.sh`.
     The legacy `~/.vct-secrets/shared/github_pat` file fallback was removed in the 0.1.7
     fork-readiness sweep (item H4). New flow is env-first via launcher's `write_project_env_files`
     plus a hub-API resolver helper.
     Action: doc rewritten in both 02-mcps and 05-install.

GAP: docs/features/06-license-and-commercial.md described `_machine_id_hash()` as `sha256(mac_address_bytes)`
     without specifying that the implementation is `uuid.getnode().to_bytes(8, "big")` — i.e.
     the 6-byte MAC zero-padded to 8 bytes. Mirror in Rust does the same.
     Action: doc clarified.

GAP: docs/features/07-architecture.md claimed "73+ trust-critical tests". Actual: 99 across 37
     test files (verified by `grep -rE '^def test_' tests/`).
     Action: doc updated to "~100 trust-critical tests".
```

## Checks NOT applied

- I did not touch the `docs/license/` sub-tree (Group C scope).
- I did not touch `docs/RELEASING.md`, `docs/LAUNCHER_SUBTREE.md`, `docs/VCT_SECRETS_PRIMITIVE.md`, `docs/DEPENDENCY_LICENSES.md`, `docs/demo_script.md` (Group D scope).
- I did not touch `README.md`, `CLAUDE.md`, `docs/GETTING_STARTED.md` (inflight PRs #178/#179).
- I did not touch other `docs/` files outside `features/`.

## Counts

- Mechanical fixes: ~32 individual edits across 8 files (INDEX + 01..07).
- Code-doc gaps surfaced: 18 distinct gaps (listed above).
