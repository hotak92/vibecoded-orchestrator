# KG auto-sync on add-project — patch summary (2026-05-12)

**Author**: Opus 4.7 (1M context), xhigh effort
**Reviewer**: Martino
**Status**: ready for review — not committed

> **Update 2026-05-12 (rev 2)**: three user-decided follow-ups applied after
> the initial round. See "Follow-up decisions" section near the bottom for
> what changed: full-width banners replacing pills on the project page; a
> "Re-sync KG" header button mirroring "Re-build code graph"; resume-after-
> crash plumbing wired at launcher boot for both task types. List-row pills
> were restored (project-list grid still uses the compact passive pill —
> only the project-page surface was promoted to banner). Two obstacles
> flagged + resolved before coding (Settings-button placement and
> running-row contract) are documented inline.

---

## Problem

When a user adds a project via the launcher GUI, VCO creates per-project
Weaviate collections (`<Project>_KnowledgeGraph`, `<Project>_Development`,
etc.) and installs the `.claude/` bundle. **But** pre-existing
`knowledge/**/*.md` and `docs/**/*.md` content in the project folder is
not synced — those files only land in Weaviate when the user opens a
Claude session and the post-file-edit hook fires on a subsequent edit.

Observed today (2026-05-12): user added SimRacing_AI via the launcher.
The project had ~58 pre-existing KG nodes. After VCO setup, the
`SimRacingAI_KnowledgeGraph` collection was empty. Manual fix:
`.claude/scripts/kg-sync --all` → 58 chunks ingested. **The launcher
add-project flow should do this automatically.**

## Approach

Mirrored — line by line — the existing `commands::codegraph::spawn_initial_build`
pattern (Gap 2, OSS launch 2026-05-12). That pattern is the
authoritative reference for "kick off a background task on project
create, persist its lifecycle in launcher.db, stream progress to the
frontend via a Tauri event, surface a status indicator in the project
page with a retry-on-failure affordance". The KG-sync feature now has
the same shape — same DB table layout, same status lifecycle, same
event-emit plumbing, same UI components, same failure-recovery UX.

No new patterns invented. Every architectural choice (subprocess launch
via `tokio::process::Command` with an arg-vec; cross-platform script
resolution following `resolve_analyzer_script`; CREATE_NO_WINDOW gate on
Windows; FK cascade on project delete; emoji-prefixed progress glyphs)
already exists in VCO_dev for the code-graph build; the kg-sync feature
borrows it intact.

## Files changed (rev 2 cumulative)

### New files (6)

| Path | Lines | Purpose |
|---|---:|---|
| `launcher/src-tauri/src/db/migrations/011_kg_syncs.sql` | 46 | Schema: `kg_syncs` table, mirrors `code_graph_builds` shape with `kg_*` / `docs_*` counters and FK cascade on project delete. |
| `launcher/src-tauri/src/db/kg_syncs.rs` | ~440 | Row CRUD (`upsert_kg_sync` / `get_kg_sync` / `KgSyncRow`); resume helpers (`list_pending_kg_syncs`, `mark_orphaned_running_kg_syncs_failed`, test-only `list_orphaned_running_kg_syncs`); 9 unit tests. |
| `launcher/src-tauri/src/commands/kg_sync.rs` | ~1160 | Core command module. `get_kg_sync_status`, `retry_kg_sync` Tauri commands; `spawn_initial_sync` background-task entry; `resume_pending_syncs` boot-time recovery; line-by-line stdout parser; cross-platform script resolution + invocation; 15 unit tests. |
| `launcher/src/lib/components/CodeGraphBuildBanner.svelte` | ~370 | Full-width project-page banner for the code-graph build. Promoted from the previous `CodeGraphBuildPill` (rev 2). Self-managed visibility (terminal states fade after 30s, failed never auto-hides), inline expand-on-click for failure details, "Retry build" button. Mirrors `.orch-banner` layout + `BrowserModeBanner` row-decoration. |
| `launcher/src/lib/components/KgSyncBanner.svelte` | ~390 | Parallel KG-sync banner. Same shape as `CodeGraphBuildBanner`. |
| `launcher/src/lib/components/CodeGraphBuildPill.svelte` | ~190 | (Rev 2) Restored — passive list-row indicator only, no popover. Lives on `routes/project/+page.svelte` (projects list grid). The full-width banner handles project-page duty. |

### Modified files (rev 2 cumulative)

| Path | Purpose |
|---|---|
| `launcher/src-tauri/src/db/migrations.rs` | Register migration 011. |
| `launcher/src-tauri/src/db/mod.rs` | `pub mod kg_syncs;` |
| `launcher/src-tauri/src/commands/mod.rs` | `pub mod kg_sync;` |
| `launcher/src-tauri/src/commands/projects_v2.rs` | Hook into `create_project_v2`: queue pending row + spawn background task. Placed after the codegraph spawn block (same race-fix discipline). |
| `launcher/src-tauri/src/lib.rs` | Register `get_kg_sync_status` + `retry_kg_sync`. **Rev 2**: also call `commands::codegraph::resume_pending_builds(...)` + `commands::kg_sync::resume_pending_syncs(...)` inside the `setup()` closure after migrations + service auto-start. Boot-log line emitted when either sweep is non-zero. |
| `launcher/src/lib/types/launcher.ts` | `KgSyncStatus` + `KgSyncView` types. |
| `launcher/src/lib/components/KgSyncPill.svelte` | (Rev 2) Restored — parallel passive list-row indicator for KG sync status, mounted next to `CodeGraphBuildPill` on the projects-list page. |
| `launcher/src/routes/project/[id]/+page.svelte` | **Rev 2**: pills removed from header `.project-meta`; banners (`KgSyncBanner` + `CodeGraphBuildBanner`) mounted above tab-nav, stacked vertically (KG sync on top — newer task — per Decision 2026-05-12); "Re-sync KG" header button added next to "Re-build code graph", mirroring `rebuildCodeGraph` end-to-end (loading state, toast on success/error, calls `retry_kg_sync`). |
| `launcher/src/routes/project/+page.svelte` | **Rev 2**: `KgSyncPill` mounted in list-row alongside the existing `CodeGraphBuildPill`. |
| `launcher/src-tauri/src/db/code_graph_builds.rs` | **Rev 2**: `#[allow(dead_code)]` removed from `list_pending_code_graph_builds`; new `list_orphaned_running_code_graph_builds` (test-only `#[cfg(test)]`) + `mark_orphaned_running_code_graph_builds_failed`; 3 new tests for the resume helpers. |
| `launcher/src-tauri/src/commands/codegraph.rs` | **Rev 2**: new `resume_pending_builds(app)` function — two-phase sweep used at launcher boot. Mirrors `kg_sync::resume_pending_syncs`. |

## Decisions (rev 2)

User picked three follow-ups on 2026-05-12 after reviewing the initial
pill-based patch:

### Decision 1 — Banners instead of pills (project page only)

Promoted the pills on the project page to full-width banners modeled on
`BrowserModeBanner` (banner row decoration) and `.orch-banner` (action-
row layout). Two parallel components (per the user's "default (b) — fewer
ripples"): `CodeGraphBuildBanner.svelte` + `KgSyncBanner.svelte`. They
stack vertically when both are active (KG sync on top — it's the newer
task in the add-project spawn order). Each self-manages visibility:

- `pending` / `running` / `failed` → always visible
- `success` / `skipped` → visible until 30s after `finished_at_iso`,
  then auto-unmounts (or the user clicks ×)
- A new run resets dismissal

Failure detail (error message + log tail) now lives inline inside the
banner (Show details / Hide details toggle) instead of a floating
popover. Retry button is on the banner header — single click recovery.

**Obstacle that surfaced during implementation**: the project**-list**
page (`routes/project/+page.svelte`) was using `<CodeGraphBuildPill
projectId compact />` to render a per-row status indicator. Replacing
that with a full-width banner would have broken the list grid (one
banner per project row → unusable). Resolved by keeping a slimmed-down
`CodeGraphBuildPill` and adding a parallel `KgSyncPill` specifically
for list-row use; both are passive (no click target, no popover) since
the project-page banner handles all action surface. Documented inline
in both pill components.

### Decision 2 — "Re-sync KG" header button (Option B)

User picked **Option B** of the three I flagged: add "Re-sync KG" to
the project **header** next to the existing "Re-build code graph"
button. Smallest diff. Settings page is NOT touched.

The new button mirrors `rebuildCodeGraph` end-to-end:
- Same `.rebuild-btn` style (already had `flex-shrink: 0`)
- Same disabled-while-running state (new `resyncingKg` $state)
- Same `invoke('retry_kg_sync', { projectId })` flow (the same Tauri
  command the banner's Retry button calls)
- Same toast convention: `toast.success('KG sync started')` on click;
  `toast.error(e)` on failure
- Same "Starting…" transient text

**Obstacle flagged before coding**: the user's original directive said
"Find the Settings page section where 'Re-build code graph' lives". It
doesn't — the rebuild button lives in the project header, not in
`routes/project/[id]/settings/+page.svelte` (which has Metadata / Bundle
/ env vars / Danger zone, no maintenance section). I stopped and asked,
user confirmed Option B (header) over Options A (new Settings section)
or C (both).

### Decision 3 — Resume-after-crash, Option α (the agent's pick)

Honored the existing design's stated intent for `list_pending_*`
functions: they remain **pending-only**. The `running`-row recovery is
explicit: a separate sweep marks them as `failed` with a clear error
message ("launcher crashed mid-run; click Retry to re-run"), and the
GUI banner then renders the failed state with a Retry button. One click
to recover. No silent re-spawn — the lifecycle break stays visible.

Plumbing:

- **`db/kg_syncs.rs`**: `list_pending_kg_syncs` (production) +
  `list_orphaned_running_kg_syncs` (test-only) +
  `mark_orphaned_running_kg_syncs_failed` (single-statement UPDATE).
- **`db/code_graph_builds.rs`**: mirrored. The pre-existing
  `list_pending_code_graph_builds` got its `#[allow(dead_code)]`
  stripped and the "TODO: wire" replaced with a "Wired at launcher boot
  (2026-05-12)" note. Added the same `list_orphaned_*` + `mark_*` pair.
- **`commands/codegraph.rs`** & **`commands/kg_sync.rs`**:
  `resume_pending_*(app)` functions, two-phase (mark stale-running as
  failed → respawn pending). Soft-fail at every step. Return
  `(swept, respawned)` for the boot-log line.
- **`lib.rs setup()`**: calls both inside the setup closure (not a
  spawned task) so the sweep lands before the GUI mounts. The functions
  return after enqueuing `tokio::spawn` — no long-lived work blocks
  setup. A single `eprintln!` summarises the sweep if non-zero.

**Obstacle flagged before coding**: the user's directive said "query
the DB for any rows in `pending` or `running` state, and re-spawn the
background tasks for them" — but `list_pending_code_graph_builds`'s
existing docstring explicitly says `running` rows are stale ghosts that
must be marked failed, NOT respawned. I stopped and asked; user picked
Option α (honor existing design — sweep `running` to `failed`, respawn
only `pending`). Done that way.

Tests covering the resume helpers:

- `kg_syncs::tests::list_pending_returns_only_pending_rows_in_started_at_order`
- `kg_syncs::tests::list_orphaned_running_returns_only_running_rows`
- `kg_syncs::tests::mark_orphaned_running_flips_status_to_failed_and_sets_error_message`
- `kg_syncs::tests::mark_orphaned_running_is_no_op_when_no_running_rows`
- `code_graph_builds::tests::list_orphaned_running_code_graph_builds_returns_only_running`
- `code_graph_builds::tests::mark_orphaned_running_code_graph_builds_flips_to_failed`
- `code_graph_builds::tests::mark_orphaned_running_code_graph_builds_no_op_when_empty`

The fixture in each module (`fresh_db_with_mixed_states` /
`fresh_db_with_mixed_build_states`) plants one row per status and
asserts (a) sweep finds only running rows, (b) sweep flips them to
failed with the error message and `finished_at` set, (c) other states
are untouched.

## Verification (rev 2)

* **`cargo check`**: clean, no new warnings (2 pre-existing warnings
  in unrelated modules).
* **`cargo test --lib`**: **550 passed, 0 failed, 1 ignored**.
  Per-module breakdown for the touched modules: `db::kg_syncs` 9
  passed (4 new resume tests); `db::code_graph_builds` 9 passed (3 new
  resume tests).
* **`svelte-check`**: **0 errors**, 38 pre-existing a11y warnings in
  unrelated routes — none from new code (banners, restored pills,
  banner-mount changes in `+page.svelte`).
* **Cross-platform**: every Rust path is `PathBuf::join`, never
  string-cat. Script picked by `cfg!(windows)` (`kg-sync.ps1` vs
  `kg-sync`). Subprocess via `tokio::process::Command` with arg-vec
  (no `cmd /c`, no `bash -c`). `CREATE_NO_WINDOW` on Windows.

## Manual test plan

### Scenario 1 — project with pre-existing KG content (primary case)

1. Rebuild the launcher: `cd launcher && cargo tauri build` (or run
   in dev: `cargo tauri dev`).
2. Pick a project folder that already has `knowledge/**/*.md` content
   but has never been registered with VCO.
3. In the launcher, click "Add project" → point at the folder.
4. **Expected**: project create returns immediately. The project page
   shows two stacked full-width banners under the header (KG sync on
   top, then code-graph build). KG banner shows
   "KG sync: scanning knowledge/ and docs/…" → "KG sync: embedding
   (12 / 58)" with a counter advancing live, terminal state
   "KG sync: indexed 58 nodes" (green). Each banner auto-hides 30
   seconds after success.
5. The project**-list** page (`/project`) shows a pair of compact
   pills next to the project name — same colours, same status,
   read-only.
6. Click "Re-sync KG" in the project page header → toast says
   "KG sync started", banner re-appears in pending → running.

### Scenario 2 — failure path (Weaviate down)

1. Stop Weaviate: `podman stop weaviate_claude`.
2. Add a project with KG content.
3. **Expected**: project create succeeds. KG banner flips to red
   "KG sync: failed". Click "Show details" → see the script's stderr
   tail. Click "Retry sync" → start Weaviate
   (`podman start weaviate_claude`), banner returns to green "KG sync:
   indexed N nodes".

### Scenario 3 — empty project (skipped path)

1. Create an empty folder, add it via the launcher.
2. **Expected**: KG banner shows yellow "KG sync: no knowledge/ or
   docs/ content to sync", with a × dismiss button. Auto-hides
   after 30s if not dismissed.

### Scenario 4 — resume-after-crash (new in rev 2)

1. Add a project with substantial KG content (~50+ nodes) and watch
   the banner go to "KG sync: embedding…".
2. **Mid-run, kill the launcher**: `pkill -9 vct-launcher` (or
   `kill -9 $(pgrep -f vct-launcher)`) — simulate a crash.
3. Restart the launcher: `npm run tauri:dev` or the bundled app.
4. **Expected**:
   * Boot log line: `[vct] resume-sweep: code-graph (running→failed: N,
     pending respawned: M); kg-sync (running→failed: 1, pending
     respawned: 0)`.
   * Open the project page. KG banner shows red "KG sync: failed"
     with error "launcher crashed mid-run; click Retry to re-run"
     under "Show details".
   * Click "Retry sync" → fresh run starts immediately.
5. Variant: kill the launcher BEFORE the run gets past the pending
   row (`pkill -9` within the first ~50ms of clicking Add Project,
   tricky to time). Boot will see `pending` in `kg_syncs` and
   auto-respawn. Banner shows "KG sync: scanning…" without user
   intervention.

### Scenario 5 — Windows / macOS

Untested on this Linux box. Cross-platform plumbing is borrowed
verbatim from `codegraph::resolve_analyzer_script` /
`codegraph::run_build_task`, both of which ship and are known to work
on macOS and Windows. The only Windows-specific code path is the
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File <ps1>`
invocation in `invocation_for`, matching `install.ps1`.

## Known limitations / trade-offs

(Items 1-6 from rev 1 unchanged — re-read those for the original
sync-mechanics caveats.)

7. **Boot-time resume runs synchronously inside `setup()`.** The two
   `resume_pending_*` calls are synchronous wrappers that enqueue
   `tokio::spawn` futures — they don't block. But the DB sweep
   (the `UPDATE kg_syncs SET ... WHERE status='running'`) does run
   under the mutex inside `setup()`. On a healthy DB this is
   single-digit ms; on a corrupted / very large DB it could
   theoretically slow boot. Soft-fail catches errors and continues.
   If this ever becomes a real concern, the sweep could move to
   `tauri::async_runtime::spawn` like `auto_start_on_boot` does. Not
   worth the complication today.

8. **Resume sweep doesn't distinguish a clean shutdown from a crash.**
   If the user `force-quit`s while a run is going (Ctrl+C, system
   shutdown, etc.), the `running` row gets marked failed on next boot
   — even if the previous run was "going to succeed had we waited".
   That's the intended behaviour per the design contract (user sees
   the broken lifecycle, clicks Retry, gets a fresh run). The kg-sync
   script is idempotent, so a re-run is a content-hash no-op at the
   Weaviate layer.

9. **Banner z-index / scroll behaviour**: banners are part of the
   normal page flow (between `header` and `tab-nav`), not sticky or
   fixed. If the user scrolls the tab content way down, the banner
   scrolls out of view too. Sticky placement would interfere with the
   `.orch-banner` layout (already in normal flow) and the tab-nav
   border. Current behaviour matches the existing `.orch-banner`
   convention; sticky is a follow-up if the user wants it.

10. **The list-row pills don't trigger the resume sweep.** They read
    `get_kg_sync_status` once on mount and update via the
    `kg-sync-progress` event. If the launcher crashed and the user
    opens the project list page without ever opening the project,
    they'll see "running" for ~50ms until the boot-sweep UPDATE
    propagates and the next `kg-sync-progress` event fires. The pill
    will then flip to "failed". Edge case, not user-noticeable in
    practice.

## Open questions

None. The three questions raised in rev 1 were all resolved by the
user's decisions (banners chosen; "Re-sync KG" added in header;
resume-after-crash implemented).

---

## Why no Python-side changes

`vco_lib/project_init.py` is the install-bundle / bootstrap-collections
helper. It does not currently run any post-install steps. The
cleanest integration point — same as for the code-graph build — is the
Rust `create_project_v2`, *after* it has called into `project_init.py`
to install the bundle.

**Bottom line**: rev 2 lands the three user-decided follow-ups on top
of rev 1's add-project KG-sync. Two obstacles surfaced before coding
were resolved by the user; one obstacle surfaced during coding
(list-row pill collision) was resolved by restoring a slimmed-down
passive pill alongside the new banner. All tests green; no new
warnings; cross-platform; no new dependencies; no changes to Python;
no changes to the kg-sync script itself. Ready for review.
