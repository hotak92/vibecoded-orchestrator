# VCO_dev — Context State

**Refreshed**: 2026-06-04 — v0.2.46 Part 2 ALL LANDED. V47-CHANGELOG commit ready to stage. v0.2.46 ready for adversarial review then push/tag.

## TL;DR — v0.2.45 + v0.2.46 status

- **v0.2.45 SHIPPED** 2026-06-03 17:39 UTC. All 6 binaries live. Tag annotates `465dd4a`, binary-refresh at `ff5e312`.
- **v0.2.46 MECHANICS COMPLETE, NOT YET PUSHED**.
  - **Part 1 — re-embed fix + RL hardening (DONE)**: all 7 V46 agents merged on `main` at `f868b4c`. 55 V46 tests pass. RL chat smoke green.
  - **Part 2 — third-party-adoption mode (ALL LANDED)**: V47-A through V47-G-final merged at `66ae2a2`. All Part 2 gates 19–27 PASS (confirmed 2026-06-04).
  - **V47-CHANGELOG (READY TO COMMIT)**: version pins confirmed 0.2.46 at all sites; CHANGELOG `[0.2.46]` block updated to `2026-06-04` with full Part 1 + Part 2 entries; `scripts/v0246-part2-pre-ship-check.sh` created with Gates 19–27.

**Next step**: Commit V47-CHANGELOG changes, then adversarial review, then push/tag v0.2.46.

### Part 2 progress (2026-06-04)
- **Venv audit complete** — 2 parallel auditors (architecture + adversarial). Reports under `.claude/context/audits/venv-*-2026-06-03.md`. Findings: 4 MEDIUM, 0 CRITICAL. Three landed as Part 1.5 (`c444ba9`): H1 (silent MCP-venv fallback warning), H2 (PYTHONPATH scrub on pip subprocesses), H3 (no-log-before-relaunch code comment).
- **sysinfo pinned** to `"0.38"` (`66df436`) — 0.39.x requires rustc 1.95; pin keeps contributors on 1.94 building. Forward-compat note in both Cargo.toml files.
- **V47-G-stub landed inline** (no agent — harness's worktree-base-divergence trap forced inline implementation on main). Contract for Wave-2 agents:
  - `args.adopt_project` / `args.no_adopt_project` / `args.adopt_project_replace_all` / `args.adopt_project_dry_run` (mutex group at install.py:2522+)
  - `_resolve_adopt_project_mode(args) -> "adopt" | "no-adopt" | "replace-all" | "dry-run" | None` (install.py:1376+)
  - `adopt_project_mode: str | None = None` optional kwarg added to `_venv_triage`, `_configure_claude_settings`. Pass-through stubs; Wave 2 wires real behavior.
  - Dispatch: in `main()` after `mode = "update" if args.update else "install"`. `"no-adopt"` → return 1 with refusal message; `"dry-run"` → return 0 with stub placeholder.
- **V47-G-stub tests**: 21/21 contract tests pass (`tests/test_v0246_v47gstub_adopt_contract.py`). V46 regression sweep clean.
- **Pending**: Wave 2 (V47-A through V47-F — 6 parallel agents). Wave 3 (V47-G-final + V47-CHANGELOG + adversarial review).
- **Pre-fanout audit**: `.claude/context/audits/v0246-part2-pre-fanout-map-2026-06-03.md` — identifies 5 merge-conflict hotspots and 3 design-doc corrections (JSONC won't work for settings.json marker; `--adopt` flag name collision avoided by using `--adopt-project`; local var `adopt_mode` already in use, new var named `adopt_project_mode`).

## v0.2.46 scope shipped (in-tag, no deferred fixes)

### V46-A — install.py CI-10 stopword fix (the headline)
- `_batch_query_weaviate_content_hashes` (install.py:5916+): drop broken `where: {Like, "%"}` clause + limit bump 1000→10000 + inspect `body["errors"]` BEFORE consuming data + saturation warning at ≥10000 rows
- `_prune_stale_kg_rows` (install.py:6095+): same pattern; ABORTS on saturation (vs WARN for diff-gate) to avoid deleting legitimate rows from truncated view
- 11 unit tests (`tests/test_v0246_v46a_stopword_fix.py` + `test_v0246_v46a_prune_fix.py`)

### V46-A-followup — batch-delete `valueText` → `valueTextArray`
- SECOND v0.2.43 bug caught by V46-B's live integration test
- `install.py:6299`: `"valueText": stale_uuids` (list) returned HTTP 400 ("cannot unmarshal array into Go struct field of type string"). Fixed to `"valueTextArray": stale_uuids`.
- Bug had been on disk since V0243-6 shipped in v0.2.43; masked because V46-A's broken fetch always returned empty stale_uuids → delete loop never had candidates.

### V46-B — live integration tests
- `tests/test_v0246_v46b_live_ci10_diff_gate.py` (7 tests)
- 5 live tests against real Weaviate (skip cleanly when unreachable)
- 2 source-inspection regression guards (run unconditionally, even without Weaviate)
- Pre-V46A merge state: 5 of 7 FAILED (the test is load-bearing)
- THE test that should have caught this bug in v0.2.42

### V46-C — pre-ship Gate 18 + 18b (live re-embed regression protection)
- `scripts/v0246-pre-ship-check.sh` cloned from v0245 + bumped version pins
- Gate 18a: `V46BLiveDiffGateTest::test_three_rows_returns_three_entries`
- Gate 18b: `V46BLivePruneTest::test_prune_finds_and_deletes_stale_rows`
- Both PASS against live Weaviate; structural fix for the "fresh-clone blind spot"

### V46-D — 10 silent-truncation footgun fixes
- Pattern A (cursor pagination, full enumeration): `maintain_knowledge_graph.py:140,381`, `search_knowledge.py:537`, `detect_duplicates.py:135`, `process_documents.py:249`
- Pattern B (`truncated: true` flag): `get_node_info.py:277`, `query_code_graph.py:411,776,845,859`, `claude_mcp_servers/weaviate_mcp/server.py:5629,5698,5841`
- 16 tests in `tests/test_v0246_v46d_truncation_fixes.py` + 3 AST regression guards

### V46-E — RL client-side hardening
- `launcher/src-tauri/src/installer_engine.rs` (+1073 lines incl. extensive tests + comments)
- C1: honor server's `tag` over client-resolved L0 version
- C1-followup: preserve client GPU-variant suffix (`-cpu`/`-cuda`/`-rocm`/`-metal`) when server returns bare patch tag
- C2: per-pull `--authfile <NamedTempFile>` via tempfile RAII; removed `podman login`/`podman logout` global-state mutation
- C3: audit-log emission — `pull_token_requested` / `_resolved` (with server_tag/client_resolved_tag/effective_tag_with_variant/tag_mismatch) / `_failed` (with HTTP code + error_class)
- C4: hard-fail on MAJOR/MINOR tag mismatch with publisher-pointing error
- C5: documented in code comments
- License key NEVER logged in full — 12-char prefix only, security-pinned by 2 levels of tests
- 9 Rust unit tests passing

### V46-F — `vco_lib/weaviate_helpers.py` reusable helper
- `check_graphql_errors(body, ctx, on_error)` and `post_graphql_safe(url, gql, ctx, on_error)` + `WeaviateGraphQLError` exception
- Centralizes the errors-array inspection pattern V46-A inlines twice
- 12 unit tests
- Currently UNUSED by install.py (V46-A inlines the pattern); v0.2.47 can refactor install.py to use the helper

### V46-G — CHANGELOG `[0.2.46]` + version pins 0.2.45→0.2.46
- 9 forward pin sites bumped: pyproject.toml, vct-module.json, launcher/package.json + lock, launcher/src-tauri/Cargo.toml + lock + tauri.conf.json, vct-hub/Cargo.toml, vct-launcher-core/Cargo.toml
- Cargo.lock cleanly bumped (only 3 vct-* packages + new `base64 = 0.22` from V46-E; NO transitive churn)
- package-lock.json cleanly bumped (2 lines only)
- CHANGELOG `[Unreleased]` EMPTY (no-deferred-fixes rule)
- `[0.2.46]` block populated; no "Known issues" subsection
- launcher/dist/*/metadata.json INTENTIONALLY NOT bumped (Release workflow regenerates post-tag)

### KG nodes extended (3 nodes, NO new nodes — per "update before create" rule)
- `knowledge/concepts/silent-zero-fallback-antipattern.md`: instance #3 (install.py GraphQL Like-%) with release archaeology + 5-layer fix + code-review heuristics
- `knowledge/concepts/mcp-loud-fail-error-pattern.md`: GraphQL `errors[]` sub-pattern added (third response shape outside connection/query-time)
- `knowledge/concepts/install-py-collection-bootstrap-bugs.md`: v0.2.46 section + release archaeology table
- All 3 nodes mutual cross-references

## Pre-existing FAILs in pre-ship-check (NOT v0.2.46 regressions, inherited from v0.2.45)

1. **`pytest tests/`**: `tests/test_rl_per_embedding_source.py` (gitignored, RL-side test) imports `rl_server` which isn't in VCO_dev venv. NOT a v0.2.46 issue.
2. **`npm audit (critical vulns found)`**: pre-existing dependabot territory (excalidraw transitive vulns). Same as v0.2.45 ship.
3. **`Working tree clean`**: pre-existing untracked files (HANDOFF-*.md, .claude/.vco-manifest.json, .claude/CONTEXT_STATE.md, etc.) + the auto-injected VCO deferral reminder in CLAUDE.md.

These 3 FAILs were ALSO present at v0.2.45 ship time; not blockers per the no-deferred-fixes rule because they pre-date v0.2.46.

## v0.2.47 follow-up backlog (19 observations from 4 reviewers)

### From Reader 1 (V46-A install.py)
- R1-1: Stale docstring `install.py:5929` (says "aggregate endpoint" + "cursor-based" — neither true)
- R1-2: False-positive saturation warning at exactly-10000-row collections (cosmetic; resolved when cursor pagination lands)
- R1-3 to R1-5: 3 optional test additions (partial-data scenario, empty-stale-uuids, malformed errors[])

### From Reader 2 (V46-D/E/F)
- R2-1: Lift V46-D's 5 inlined cursor-pagination loops into `vco_lib/weaviate_helpers.py`
- R2-2: `process_documents.py` delete-then-replace is O(n²) for >5K-chunk docs (acceptable today)
- R2-3: Refactor `install.py:5972,6201` inline errors-check to use V46-F's `post_graphql_safe` helper
- R2-4: V46-E `(401, _)` with unknown error code → "unknown" class (forensic aggregation could improve)
- R2-5: V46-F `WeaviateGraphQLError.__init__` could harden against `[None]` / `[str]` in errors list (theoretical)
- R2-6: V46-E `username` with `:` colon (theoretical; GitHub usernames can't contain colon)

### From Reader 3 (V46-B/C/G + KG)
- R3-1: Pre-ship gate numbering skips 26 (jumps 25→27)
- R3-2: V46-A prune-fix test file not in Gate 27 collectibility list
- R3-3: `[[auto-restart-vs-banner-UX]]` WikiLink missing target (pre-existing from v0.2.17)

### From Adversarial (M1-M5)
- M1: Unit-test gap for `valueTextArray` (DELETE body shape not asserted in unit tests; only live integration covers)
- M2: `merge_server_tag_with_client_variant` trusts client tag verbatim (catalog-driven variants for v0.2.47)
- M3: `data: null` falls through to generic exception log (non-diagnostic; could improve observability)
- M4: V46-D Pattern A mid-iteration failure presents as empty result (pre-existing pattern, not v0.2.46 regression)
- M5: Authfile creation failure has audit-log gap (`?` propagation skips explicit failure audit row)

### Plus 2 low-priority (Adversarial L1-L2)
- L1: V46-B test-collection cleanup leak under SIGKILL'd setUp (orphan `V46BTest*` collections; vanishingly small odds)
- L2: `classify_tag_mismatch` parses `v0.2.8` same as `0.2.8` (loose semver; unlikely real-world impact)

## Git state (DO NOT PUSH per user instruction)

| Ref | SHA |
|---|---|
| Local main HEAD | `f868b4c` (V46-G) |
| origin/main | `ff5e312` (v0.2.45 binary-refresh) |
| vco_upstream/main | `ff5e312` |
| public/main | `ff5e312` |
| Tag v0.2.45 | `465dd4a` (source) |
| v0.2.45 binary-refresh auto-commit | `ff5e312` |

**12 commits ahead of vco_upstream/main**:
```
f868b4c v0.2.46 V46-G: CHANGELOG [0.2.46] + version pins
9185044 Merge V46-C: pre-ship-check v0246 + Gate 18
5e1de45 v0.2.46 V46-C: pre-ship-check script + Gate 18
e3e133a v0.2.46 V46-A-followup: fix batch-delete valueText → valueTextArray
c7ef2a3 Merge V46-E: RL client-side hardening
97305fa Merge V46-D: 10 silent-truncation footguns
a7d0d96 Merge V46-B: live integration tests
71afc0c Merge V46-F: vco_lib/weaviate_helpers.py
fa53fe9 Merge V46-A: install.py CI-10 stopword fix
f95903e v0.2.46 V46-E: RL client-side hardening
f737153 v0.2.46 V46-D: 10 silent-truncation footguns
093364f v0.2.46 V46-B: live integration tests
fbda731 v0.2.46 V46-F: GraphQL errors-array helper
ca847fb v0.2.46 V46-A: install.py CI-10 stopword fix
```

## What the user wanted to audit before push

User instruction (2026-06-03): "do not push, instead prepare a self-handoff + update context_state and KG to save all useful context as I'll compact your context. This because I'll then want to audit (and maybe update) a few things regarding venvs"

**The venv audit context**:
- V45-A introduced `_ensure_running_under_mcp_venv()` in install.py that self-relaunches under `claude_mcp_servers/.venv` when `weaviate` isn't importable
- v0.2.46 didn't touch the venv self-relaunch logic
- Pre-existing setup: VCO_dev has 2 venvs — `.venv` (project root) + `claude_mcp_servers/.venv` (MCP subprocesses)
- vco_lib/project_init.py has `_resolve_venv_python_for_install` that picks the right one
- Bug 1 from `install-py-collection-bootstrap-bugs.md` historical: "Venv path mismatch in `_seed_weaviate` — looked for `claude_mcp_servers/.venv/bin/python`, actual was `PROJECT_ROOT/.venv/bin/python`"
- User may want to audit: which venv each subprocess uses, whether the v0.2.46 changes interact with the venv lookup, whether install.py's V45-A self-relaunch is robust under V46-E's new `--authfile`/`tempfile` Rust paths

## RL chat coordination state (post-v0.2.46)

- Supabase secret `GHCR_PAID_TAG_DEFAULT=0.2.8` SET + `rl-artifact-url` redeployed by us
- Gateway verified returns `tag: "0.2.8"`
- RL chat ran end-to-end smoke + confirmed `podman pull :0.2.8-cpu` AND `:0.2.8-cuda` both succeed
- PAT validity confirmed for all variants
- Architectural clarification accepted by RL chat: gateway returns BASE tag, launcher appends `-cpu`/`-cuda`/`-rocm` client-side via V46-E's `merge_server_tag_with_client_variant`
- Pull-back on L0-catalog-tie-in: not needed for variant resolution (only catalog-version-drift; defer to v0.2.47)
- Coordination via vct-coordination `claude-orchestrator` routing identity (NOT `rl-rl` which doesn't exist)

### Stuck `module_installs` rows on user's machine (cleared by V46-E post-tag)
- SimRacing_AI / VibeCoded Orchestrator (root) / SD15 / Instambul1860: all RL v0.2.7 stuck in `error` or `installed+last_error`
- Will clear via V44-G4 auto-retry after v0.2.46 tag + launcher restart

## CRITICAL — resume protocol for future-Claude (after compaction)

1. **DO NOT auto-push**. The user explicitly held v0.2.46 at "ready to tag" for a venv audit.
2. **Read this CONTEXT_STATE.md fully** + `HANDOFF-2026-06-03-v0.2.46-READY-TO-TAG.md` (next to this file).
3. **Verify state**: `git rev-parse HEAD` should equal `f868b4c`. `git log --oneline ff5e312..HEAD | wc -l` should be `14`.
4. **Do not run any further v0.2.46 review/test cycles** — they all completed GO HIGH.
5. **Wait for user's instruction** on venv audit findings + whether to proceed to push, or whether to apply venv-related changes first.

## Documentation written this session

- `HANDOFF-TO-RL-CHAT-2026-06-03-v0.2.46-PLAN.md` (initial handoff to RL chat with 4 server-side asks)
- `HANDOFF-TO-RL-CHAT-2026-06-03-RESPONSE-v0.2.46-STATUS.md` (response after RL smoke green)
- `.claude/context/plans/rl-response-v0.2.46-handoff-2026-06-03.md` (RL chat's response, read-only ref)
- `.claude/context/plans/v0.2.46-design-2026-06-03.md` (v0.2.46 design + fanout plan)
- `HANDOFF-2026-06-03-v0.2.46-READY-TO-TAG.md` (this session's self-handoff — to be written after this file)
- 3 KG nodes extended (silent-zero-fallback, mcp-loud-fail, install-py-collection-bootstrap-bugs)

## Discipline locks carried forward (v0.2.45 → v0.2.46)

1. **No release with deferred fixes** (CLAUDE.md, post-v0.2.41 retrospective) — UPHELD: 0 FIX-NOW, all reviewers GO
2. **Release-branch-first** — pending user OK to push
3. **No auto-destroy**: Weaviate/launcher.db/embeddings preserved — UPHELD (no mass deletes in V46)
4. **Pre-ship script gate**: 24 PASS / 3 pre-existing FAIL — UPHELD
5. **3+1+1 multi-Opus pre-tag review** — UPHELD (R1/R2/R3/Adversarial all GO HIGH)
6. **Worktree-isolated fanout** with explicit base SHA verify-or-abort — UPHELD
7. **Verify agent-claimed deliverables** before merge — UPHELD (re-ran V46 tests post-merge, caught batch-delete bug → V46-A-followup)
8. **origin/main divergence trap**: avoided (no force-push needed this cycle)
9. **Tag the binary-refresh commit**: v0.2.46 not yet tagged — when pushed, follow v0.2.45 pattern
10. **NEW for v0.2.46**: Live integration tests against real Weaviate are MANDATORY for any Weaviate-touching helper (caught the v0.2.42-v0.2.45 recurrence; codified in V46-B + Gate 18a/18b)
</content>
</invoke>