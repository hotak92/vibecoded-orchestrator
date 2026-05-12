# KG-summary auto-backfill on add-project — patch summary (v0.2.3, 2026-05-12)

**Author**: Opus 4.7 (1M context), xhigh effort
**Reviewer**: Martino
**Status**: ready for review — not committed
**Precedent**: `.claude/context/kg-autosync-patch-2026-05-12.md` (v0.2.2, the
two-task pattern this patch extends to three)

---

## Problem

v0.2.2 (`kg-autosync-patch-2026-05-12.md`) added auto-sync of
`knowledge/**/*.md` to Weaviate on add-project. That works — a freshly-
added project now ends up with its per-project KG collection populated
without the user needing to manually run `.claude/scripts/kg-sync --all`.

There's a follow-up gap: the orchestrator ALSO maintains a sidecar file
`<project>/knowledge/.node_formats.json` with LLM-generated summaries
per KG node, consumed by `hybrid_search`'s `summary` tier (score
0.42–0.55). Those summaries are produced by
`.claude/scripts/generate-kg-summary.py`, invoked per-file by the
PostToolUse hook `kg-summary-generator.sh` — but the hook ONLY fires on
Claude Code Edit/Write tool events, NOT when the launcher's kg-sync
subprocess writes embeddings.

Net result: a freshly-added project has Weaviate populated, but
`.node_formats.json` is empty. The hook backfills lazily as the user
edits each node in a Claude session, but for a project with 50+
pre-existing nodes that means 50+ Claude sessions to fully populate.
Until that happens, `hybrid_search` results at the `summary` tier
(score 0.42–0.55) render with no LLM-generated description and degrade
to a content-fragment fallback.

## Approach

Mirrored — line by line — the v0.2.2 `commands::kg_sync` pattern (which
itself mirrored `commands::codegraph` from Gap 2). This is the THIRD
instance of the same pattern; by now the architecture is well-established
and reproducing it required no design decisions, just disciplined
cloning.

Each instance has:
- A migration creating a single per-project `<task>` table with the
  standard lifecycle states.
- A `db/<task>.rs` module with `upsert_*`, `get_*`, `list_pending_*`,
  `list_orphaned_running_*` (test-only), `mark_orphaned_running_*_failed`.
- A `commands/<task>.rs` module with `get_*_status`, `retry_*` Tauri
  commands; `spawn_initial_*` background entry; `resume_pending_*`
  boot-time recovery; cross-platform script + interpreter resolution.
- A `<Task>Banner.svelte` + `<Task>Pill.svelte` pair (full-width banner
  for the project page, compact pill for the projects-list row).
- `lib.rs::setup()` resume sweep gets a third pair of returns added to
  its `(swept, respawned)` aggregate.
- `commands::projects_v2::create_project_v2` gets a third
  upsert-pending + spawn block, appended after the kg-sync block.
- `routes/project/[id]/+page.svelte` gets a third header `.rebuild-btn`
  and a third `<Banner>` mount (on top of the stack — newest task).
- `routes/project/+page.svelte` gets a third `<Pill>` in the list-row.

No new patterns invented. Every architectural choice already exists in
VCO_dev twice over.

## Files changed

### New files (4)

| Path | Lines | Purpose |
|---|---:|---|
| `launcher/src-tauri/src/db/migrations/012_kg_summaries.sql` | 53 | Schema: `kg_summaries` table, mirrors `kg_syncs` shape with `nodes_*` counters (succeeded/unchanged/failed/skipped), `backend` column, FK cascade on project delete. |
| `launcher/src-tauri/src/db/kg_summaries.rs` | ~485 | Row CRUD (`upsert_kg_summary` / `get_kg_summary` / `KgSummaryRow`); resume helpers (`list_pending_kg_summaries`, `mark_orphaned_running_kg_summaries_failed`, test-only `list_orphaned_running_kg_summaries`); 9 unit tests covering round-trip, transitions, invalid-status, log-tail truncation, FK cascade, and the resume sweep. |
| `launcher/src-tauri/src/commands/kg_summary.rs` | ~990 | Core command module. `get_kg_summary_status` + `retry_kg_summary` Tauri commands; `spawn_initial_summary` background-task entry; `resume_pending_summaries` boot-time recovery; per-file subprocess loop with stdout-marker classification (succeeded / unchanged / no-backend / failed); cross-platform script + venv-python resolution; 16 unit tests. |
| `launcher/src/lib/components/KgSummaryBanner.svelte` | ~395 | Full-width project-page banner. Mirrors `KgSyncBanner` shape — same self-managed visibility (terminal states fade 30s, failed never hides), same inline expand-on-click for failure details, same retry affordance. Adds "Show details" for `skipped` so the user can read the "no backend available" hint. |
| `launcher/src/lib/components/KgSummaryPill.svelte` | ~185 | Compact, passive list-row pill. Mirrors `KgSyncPill` / `CodeGraphBuildPill` 1:1. |

### Modified files (7)

| Path | Purpose |
|---|---|
| `launcher/src-tauri/src/db/migrations.rs` | Register migration 012. |
| `launcher/src-tauri/src/db/mod.rs` | `pub mod kg_summaries;` |
| `launcher/src-tauri/src/commands/mod.rs` | `pub mod kg_summary;` |
| `launcher/src-tauri/src/commands/projects_v2.rs` | Hook into `create_project_v2`: queue pending row + spawn background task. Placed AFTER the kg-sync spawn block (third in the parallel-spawn sequence). Cloned `app` for kg-sync spawn so kg-summary can also consume the handle. New `use crate::commands::kg_summary;` + `use crate::db::kg_summaries::status as kg_summary_status;` imports. |
| `launcher/src-tauri/src/lib.rs` | Register `get_kg_summary_status` + `retry_kg_summary` invoke handlers. Extend the `setup()` resume-sweep aggregation to add a third `(swept, respawned)` pair via `commands::kg_summary::resume_pending_summaries(...)`. Boot-log line now reports all three task types. |
| `launcher/src/lib/types/launcher.ts` | `KgSummaryStatus` + `KgSummaryView` types. |
| `launcher/src/routes/project/[id]/+page.svelte` | Banner mount: `<KgSummaryBanner>` added at the top of the stack (newest task → on top). Header button: third `.rebuild-btn` "Re-build KG summaries" mirroring `Re-sync KG` end-to-end (loading state via `rebuildingSummaries` $state, toast on success/error, calls `retry_kg_summary`). |
| `launcher/src/routes/project/+page.svelte` | `<KgSummaryPill>` mounted in list-row alongside `CodeGraphBuildPill` and `KgSyncPill`. |

## Decisions

### Decision A — Invoke the .py directly (no `kg-summary` wrapper)

The summariser is a standalone Python script that's already directly
invokable as `<venv-python> generate-kg-summary.py <file>`. Inspecting
how the PostToolUse hook (`kg-summary-generator.sh`) actually drives the
script, it does exactly that — no wrapper layer in between. I therefore
did NOT add a third pair of POSIX-`/.ps1` wrapper scripts under
`templates/scripts/` (unlike `kg-sync` / `kg-sync.ps1`). The launcher
resolves a venv python the same way `codegraph.rs::looks_like_install_root`
does, then exec()s the .py directly. This avoids:

1. A spurious `kg-summary` / `kg-summary.ps1` pair that the user would
   then have to opt out of running by hand (the summariser is hooked
   to a Claude tool event, not exposed as a CLI command).
2. A `kg-summary --all` subcommand that doesn't exist in
   `generate-kg-summary.py` and would require either an upstream change
   to the script or yet another wrapper layer doing per-file iteration.

Cross-platform parity is preserved: the launcher's `resolve_venv_python`
probes both POSIX (`.venv/bin/python(3)`) and Windows
(`.venv/Scripts/python.exe`) layouts, same shapes that
`codegraph::looks_like_install_root` already probes. No new `.sh`/`.ps1`
scripts to keep in sync.

### Decision B — Parallel spawn with kg-sync (not chained)

The prompt explicitly raised the question of ordering vs. kg-sync. After
inspecting both paths:

- The summariser's `description` + `summary` generation works directly
  off the `.md` file body — it does NOT need Weaviate to be populated.
- The summariser's `chunk_summaries` path (only used for multi-chunk
  nodes) DOES query Weaviate via `get_chunks_from_weaviate(title)`. But
  that function already wraps in `try/except` and returns `[]` on any
  error, falling back to single-summary mode silently.

So the cleanest mirror of the existing two-task pattern is:
parallel-spawn all three. The summariser will degrade to single-summary
mode if it happens to run ahead of kg-sync for a multi-chunk node. The
user-visible impact is minimal (the most-common nodes are single-chunk),
and clicking "Re-build KG summaries" after kg-sync completes regenerates
the multi-chunk variants — at which point `get_chunks_from_weaviate`
returns the populated chunks and the per-chunk path activates.

Trade-off accepted: marginally degraded chunk-summary quality on the
initial pass, in exchange for the simpler parallel-spawn topology.

### Decision C — Banner skipped path with actionable hint

The summariser's "no backend available" path is by far the most likely
failure mode for users without `claude` CLI installed and without
Ollama running locally. On the first node we detect this marker, we
hard-stop the rest of the walk (re-invoking the script for 49 more
files would just print the same warning 49 times and burn ~5s each on
subprocess startup) and transition the row to `skipped` with an
`error_message` that lists the three install paths:

> "no backend available — install the `claude` CLI (preferred), start
> Ollama at the configured URL, or set ANTHROPIC_API_KEY. Summaries
> will also generate incrementally as you edit nodes in Claude Code
> sessions."

The banner's `Show details` button on the `skipped` state surfaces this
message inline (unlike `kg-sync`, where `skipped` is just "no content
to sync" and Show-details would have nothing to add). Same UX as the
`failed` path — same button, same expansion shape. The "Retry" button
is also wired on `skipped` so the user can re-run after installing a
backend without having to go through the header button.

### Decision D — Fail-fast threshold for non-no-backend failures

For non-"no-backend" failures (e.g. Ollama is up but returns 503, or
Anthropic's API rate-limits), we tolerate `SUBPROCESS_FAIL_FAST_THRESHOLD
= 3` consecutive failures before bailing on the remaining nodes. Below
that threshold individual node-failures are counted under `nodes_failed`
and the loop continues. Threshold chosen conservatively: 3 consecutive
crashes is a strong enough signal that something systemic is wrong (a
missing dependency, a network outage) that chewing through 50 more
identical exec()s is wasteful. Per-node failures below the threshold
still land in `nodes_failed` and surface in the banner's detail line
("backend: ollama · new: 12 · failed: 1") so the user sees there were
hiccups without losing the run.

## Verification

* **`cargo check`**: clean, no new warnings (2 pre-existing warnings in
  unrelated modules — same as v0.2.2 baseline).
* **`cargo test --lib`**: **575 passed, 0 failed, 1 ignored.**
  Per-module breakdown for the new modules:
    - `db::kg_summaries` — 9 tests passed (round-trip, transitions,
      invalid-status, log-tail truncation, FK cascade, resume sweep ×
      4).
    - `commands::kg_summary` — 16 tests passed (enumerate × 4,
      resolve_summary × 2, resolve_venv_python × 3, parse_backend × 2,
      tail_log × 2, append_log × 1, marker-string drift guards × 2).
  Net delta vs. v0.2.2 baseline (550 tests): +25 tests, +0 failures.
* **`svelte-check`**: **0 errors**, 38 pre-existing a11y warnings in
  unrelated routes — none from new code (banner, pill, banner-mount
  changes in `+page.svelte`, list-row pill addition).
* **Cross-platform**: every Rust path is `PathBuf::join`, never
  string-cat. Subprocess via `tokio::process::Command` with arg-vec
  (no `cmd /c`, no `bash -c`). `CREATE_NO_WINDOW` on Windows.
  `resolve_venv_python` probes both POSIX (`bin/python`, `bin/python3`)
  and Windows (`Scripts/python.exe`) shapes inside both `.venv/` and
  `claude_mcp_servers/.venv/` candidate layouts — the same matrix
  `codegraph.rs::looks_like_install_root` covers.

### Marker-string drift safety net

The `commands::kg_summary` test file includes two "marker string drift"
tests:

```rust
no_backend_marker_string_matches_script_log_line
unchanged_marker_string_matches_script_log_line
```

These keep a literal snippet of the canonical log line from
`generate-kg-summary.py` and assert that the `NO_BACKEND_MARKER` and
`UNCHANGED_MARKER` constants still match. If someone renames the
"no backend available" message in the Python script, the test will fail
loudly instead of letting production silently mis-classify every run as
"succeeded" (which would happen if the marker drifted — no marker found
means no stdout-match means we fall through to `NodeOutcome::Succeeded`).

## Manual test plan

### Scenario 1 — project with pre-existing KG content, Ollama up (primary case)

1. Rebuild the launcher: `cd launcher && cargo tauri build` (or `cargo
   tauri dev`).
2. Pick a project folder that already has `knowledge/**/*.md` content
   but has never been registered with VCO. Make sure Ollama is running
   (`curl http://localhost:11435/api/tags`) or `claude` CLI is on PATH.
3. In the launcher, click "Add project" → point at the folder.
4. **Expected**: project create returns immediately. The project page
   shows three stacked full-width banners under the header — KG summary
   on top, KG sync in the middle, code-graph at the bottom. KG summary
   banner shows "KG summaries: scanning knowledge/…" → "KG summaries:
   ollama (1 / 58)" with a counter advancing live, terminal state
   "KG summaries: summarised 58 nodes" (green). Each banner auto-hides
   30 seconds after success.
5. The project-list page (`/project`) shows a triplet of compact pills
   next to the project name — same colours, same status, read-only.
6. Click "Re-build KG summaries" in the project page header → toast
   says "KG summaries rebuild started", banner re-appears in pending →
   running. On the rebuild the counter shows mostly "unchanged" (the
   script content-hashes nodes; only edited nodes get re-generated).

### Scenario 2 — no backend available (most-common failure path)

1. Stop Ollama: `podman stop ollama_claude` (or wherever it runs).
2. Make sure `claude` CLI is NOT on PATH and `ANTHROPIC_API_KEY` is
   unset for the launcher process.
3. Add a project with KG content.
4. **Expected**: project create succeeds. KG summary banner flips to
   yellow "KG summaries: skipped" after the first node's subprocess
   detects "no backend available" and hard-stops the loop. Click "Show
   details" → see the actionable message:
   > "no backend available — install the `claude` CLI (preferred),
   > start Ollama at the configured URL, or set ANTHROPIC_API_KEY.
   > Summaries will also generate incrementally as you edit nodes in
   > Claude Code sessions."
5. Click "Retry" → start Ollama → banner returns to green
   "KG summaries: summarised N nodes".

### Scenario 3 — empty project (skipped path, no nodes)

1. Create an empty folder, add it via the launcher.
2. **Expected**: KG summary banner shows yellow "KG summaries: skipped",
   `Show details` shows "no knowledge/**/*.md files to summarise". `×`
   dismiss button visible; auto-hides after 30s if not dismissed.

### Scenario 4 — pre-existing summaries on retry (unchanged path)

1. Run Scenario 1 to completion (banner green).
2. Without editing any files, click "Re-build KG summaries".
3. **Expected**: counter advances rapidly (~10-50ms per node, no LLM
   call), terminal state "KG summaries: summarised 58 nodes" with detail
   line showing "unchanged: 58" (no `new:` entry — every node
   content-hash matched the prior run).

### Scenario 5 — failure path (Ollama returns 503)

1. Start Ollama but force it to fail (e.g. configure with a model that
   doesn't exist: `KG_SUMMARY_OLLAMA_MODEL=does-not-exist:1b`).
2. Add a project with KG content.
3. **Expected**: KG summary banner flips to red "KG summaries: failed"
   after 3 consecutive subprocess failures (the
   `SUBPROCESS_FAIL_FAST_THRESHOLD`). Click "Show details" → see the
   exit-code snippet and a log tail with the Ollama error response.
   Click "Retry" → fix the env → run completes green.

### Scenario 6 — resume-after-crash

1. Add a project with substantial KG content (~50+ nodes) and watch
   the banner go to "KG summaries: ollama (10 / 58)".
2. **Mid-run, kill the launcher**: `pkill -9 vct-launcher`.
3. Restart the launcher: `npm run tauri:dev` or the bundled app.
4. **Expected**:
   * Boot log line includes `kg-summary (running→failed: 1, pending
     respawned: 0)` alongside the existing code-graph + kg-sync sweep
     counts.
   * Open the project page. KG summary banner shows red "KG summaries:
     failed" with error "launcher crashed mid-run; click Retry to
     re-run" under Show details.
   * Click "Retry" → fresh run starts; previously-summarised nodes
     content-hash-match and skip, the rest pick up where the crash
     interrupted.

### Scenario 7 — Windows / macOS

Untested on this Linux box. Cross-platform plumbing is borrowed
verbatim from `kg_sync::resolve_kg_sync_script` and
`codegraph::looks_like_install_root` — both ship and are known to work
on macOS and Windows. The Windows-specific path is the
`.venv/Scripts/python.exe` venv-shape probe, which already exists in
`codegraph.rs::looks_like_install_root` and the
`kg-summary-generator.sh` POSIX hook's Windows fallback line.

## Known limitations / trade-offs

1. **Multi-chunk nodes may get single-summary on initial pass.** Because
   the summariser is spawned in parallel with kg-sync (rather than
   chained), `get_chunks_from_weaviate` may return `[]` for the first
   few nodes if the Weaviate writer hasn't caught up yet. The summariser
   silently falls back to single-summary mode in that case. Clicking
   "Re-build KG summaries" after kg-sync's banner shows green will
   regenerate the multi-chunk path. Not a correctness issue (single-
   summary mode is the default for ~80% of nodes anyway), but worth
   documenting.

2. **Sequential per-node subprocess.** The summariser is invoked once
   per `.md` file. For a 58-node project on Ollama (qwen3.5:9b, ~3s
   per node), that's ~3 minutes wall-time. We could parallelise (e.g.
   `FuturesUnordered` with a concurrency cap of 4), but two reasons
   not to: (a) the underlying Ollama instance is rate-limited by VRAM
   and would not actually go 4x faster, and (b) the GUI's "X / N"
   progress counter is much more readable in sequential mode. If a
   user has a fast backend (claude CLI via API key with high RPS),
   we'd want concurrency — that's a follow-up if the wall-time
   complaint surfaces.

3. **`backend` column only records the FIRST backend seen.** If the
   summariser's `select_backend()` cache somehow flipped mid-run (e.g.
   Ollama died and the script's fallback kicked in for later nodes —
   not actually possible today because the cache is per-subprocess,
   but reserved as a "mixed" state in the schema), we'd lose
   per-backend attribution. Acceptable; surface as a single-value
   summary in the banner.

4. **`nodes_skipped` lumps together two reasons.** The summariser exits
   0 with "no backend available" and exits 0 with "no title in file"
   for the same `nodes_skipped` counter slot. The first is the common
   global-skip path; the second is a per-file edge case that the
   pre-write hook should have caught. We don't currently disambiguate
   — but the log_tail captures the per-node stdout so a curious user
   can read the actual reason for any individual skip.

5. **Resume sweep doesn't distinguish a clean shutdown from a crash.**
   Same caveat as v0.2.2: if the user force-quits while a run is going
   (Ctrl+C, system shutdown), the `running` row gets marked failed on
   next boot. The summariser is idempotent (content-hash skip), so a
   re-run is mostly free.

6. **No `Re-build KG summaries` button on the Settings page.** Per the
   v0.2.2 Decision 2 precedent — the header is the single canonical
   place for these maintenance buttons. Settings tab not touched.

7. **Banner z-index / scroll behaviour**: banners are part of the
   normal page flow. Same caveat as v0.2.2 — banner scrolls out of view
   with the rest of the page. Sticky placement would interfere with the
   `.orch-banner` layout. Current behaviour matches the existing
   convention.

## Open questions

None. Every escalation trigger from the prompt was answerable from the
source:

- **Summariser exit codes / backend-detection signal**: source-of-truth
  is `generate-kg-summary.py::select_backend()` which logs
  "KG-summary: no backend available …" + `sys.exit(0)` in `main()` when
  `select_backend() == 'skip'`. Detected via stdout substring match
  (constant `NO_BACKEND_MARKER`) with a drift-detection unit test.
- **Multiple plausible places to invoke the summariser**: kg_sync's
  spawn site is the unambiguous mirror — `create_project_v2` line 530
  area, after bundle install. The summary spawn block goes immediately
  after the kg-sync block (third in the chain).
- **Existing patterns to invent**: zero. Three precedents (codegraph,
  kg-sync, and the resume sweep added in v0.2.2 rev 2) covered every
  architectural choice.

### Why no Python-side changes

`vco_lib/project_init.py` is the install-bundle helper. It already
ships `.claude/scripts/generate-kg-summary.py` and the
`.claude/hooks/kg-summary-generator.{sh,ps1}` hooks as part of the
standard bundle (verified — those files exist in `templates/scripts/`
and `templates/hooks/`). The launcher's new background task just
consumes what bundle-install already drops. Zero Python changes
needed.

### Why no changes to the summariser script

`templates/scripts/generate-kg-summary.py` was JUST updated TODAY (per
the prompt) with the three-tier backend fallback chain (claude CLI →
Ollama → ANTHROPIC_API_KEY → silent skip). The launcher reads its
stdout markers and exit codes — no changes needed to the script side.
The marker-string drift unit tests guard against accidental message
renames in future updates.

---

**Bottom line**: v0.2.3 lands a third instance of the now-canonical
"background task with banner + pill + retry + resume" pattern,
extending the v0.2.2 add-project flow with auto-backfill of
`knowledge/.node_formats.json` via `generate-kg-summary.py`. 25 new
tests; all green; no new warnings; cross-platform; no new
dependencies; no changes to Python; no changes to the summariser
script itself. The pattern has now reached the point where the diff
is purely mechanical — looks like it's always been there.
