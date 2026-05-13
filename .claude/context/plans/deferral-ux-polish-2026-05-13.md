# Deferral UX polish — sprint plan (2026-05-13)

**Trigger**: SD15 agent wrote a thorough 580-line handoff
(`.claude/context/sd15-install-handoff-2026-05-13.md`) listing 11
gaps/observations in VCO's deferral system, surfaced when SD15 (a
project registered by a pre-v0.2.0 orchestrator) was ported via
v0.2.3's add-project flow.

**Critical filter applied (2026-05-13, coordinator)**: SD15 agent
was working from a single legacy-port data point. Not every item is
a real VCO bug; some are design intent the agent didn't recognize.
This plan covers the 5 items that hold up against reading VCO's
actual code, ranked by impact-to-effort.

---

## Items SHIPPED IN-SCOPE for this sprint

### 1. Gap 11 — `UPDATE_DEFERRED.md` not deleted after `--update --force` resolves it

**Severity**: real bug. **Effort**: ~30 LOC + 1 test.

`vco_lib/project_init.py::install_bundle` (around line 2933) only
calls `_emit_skipped_existing_deferral` when `skipped_existing_paths`
is non-empty AFTER the run. If `--force` resolves all preservation
cases, `skipped_existing_paths` is empty post-run, so the function
is never called, and **the existing stale `UPDATE_DEFERRED.md` is
NOT touched**.

`DeferralReport.write(folder)` already has the right semantics: with
empty entries it deletes the file. The emit-functions DO call
`DeferralReport.read(folder)` then mutate, then write — so the file
WOULD be unlinked if those functions were called with empty inputs.
The bug is that they're guarded behind a non-empty check.

**Fix**: at the end of `install_bundle`, unconditionally call a
"reconcile and trim" pass that re-reads the on-disk
`UPDATE_DEFERRED.md`, removes any entries whose `condition_id`
matches conditions the current install just resolved (e.g.
`bundle_skipped_existing_files`, `bundle_user_modified_files`), and
writes back (or unlinks if empty).

**Verification**: smoke test the SD15 case — current state has a
stale `UPDATE_DEFERRED.md` with `bundle_skipped_existing_files` entry
referencing 36 files that were `--force`-overwritten. After fix +
re-run of the same install command, the file should disappear.

**Code locations**:
- `vco_lib/project_init.py::install_bundle` line ~2933
- `vco_lib/deferral_report.py::DeferralReport.write` (no changes
  needed — semantics already correct)

---

### 2. Gap 10 — `UPDATE_DEFERRED.md` is invisible to future Claude sessions

**Severity**: highest UX value. **Effort**: ~80 LOC across 2 files +
3 tests.

When VCO writes a deferral, future Claude sessions opening the project
don't know to read it. The user has to remember it exists.

**Fix**: when `DeferralReport.write` writes a non-empty deferral,
ALSO inject (or update) a wrapped block into the project's
`.claude/CLAUDE.md`:

```markdown
<!-- vco-deferral-reminder-begin -->
**Pending VCO action**: `.claude/context/UPDATE_DEFERRED.md` exists.
Read it at session start — it contains commands to resolve unresolved
VCO install actions.

To remove THIS reminder block: once the deferral is resolved (e.g.
via `--update --force`), VCO's next install run will delete
UPDATE_DEFERRED.md AND strip this block. Manual cleanup if needed:
remove everything between `<!-- vco-deferral-reminder-* -->` markers.
<!-- vco-deferral-reminder-end -->
```

When `DeferralReport.write` unlinks the deferral file (per Gap 11's
fix), it ALSO strips the wrapped block from CLAUDE.md.

**Idempotency contract**: re-running an install with the same
deferral overwrites the wrapped block; does not duplicate.

**Code locations**:
- `vco_lib/deferral_report.py::DeferralReport.write` — extend with
  `_ensure_claude_md_reminder()` / `_strip_claude_md_reminder()` calls
- New helpers in `deferral_report.py` (single-purpose, easy to test)

**Tests**:
- Write deferral → CLAUDE.md gets the block once.
- Write deferral again with same content → block not duplicated.
- Write empty deferral → block stripped, deferral file unlinked.
- CLAUDE.md missing → helper creates a minimal one with just the
  block (or no-op + log warning; pick whichever is least surprising).

---

### 3. Gap 2 — File list truncated at 20, no way to see the rest

**Severity**: real but minor. **Effort**: ~15 LOC.

`_format_file_list_md(paths, cap=20)` hard-caps. With 36+ files the
trailing entries are hidden.

**Fix**: replace the silent truncation with:
- Inline list up to 30 entries (slight bump).
- Write the FULL list to a sibling file:
  `.claude/context/UPDATE_DEFERRED_files.txt` (one path per line).
- Reference the sibling file in the deferral entry: "Full list: see
  `.claude/context/UPDATE_DEFERRED_files.txt` (N files)".

**Code locations**:
- `vco_lib/project_init.py::_format_file_list_md` — accept an
  optional `full_list_target: Path` arg; when set and len > cap,
  write the full list to that path and mention it in the rendered
  text.
- All call sites of `_format_file_list_md` (currently two) — pass
  the sibling-file path.

---

### 4. Gap 7 — External `orchestrator_root` path leaked in `command_to_apply`

**Severity**: portability bug. **Effort**: ~5 LOC.

The `python -m vco_lib.project_init install-bundle ... --orchestrator-root
'/home/martino/Desktop/PROGETTI/VCO_dev'` command embeds the user's
local launcher clone path. Not portable across machines / re-clones.

**Fix**: emit `$VCT_ORCHESTRATOR_ROOT` instead of the literal path.
The env var is already set in `.claude/env` for every VCO-installed
project. Document the env var requirement in the command's preceding
prose ("Run from a shell where `.claude/env` has been sourced, or
prepend `VCT_ORCHESTRATOR_ROOT=/path/to/VCO_dev`").

**Code locations**:
- `vco_lib/project_init.py` — search for `--orchestrator-root` in
  command-string builders; replace literal `orchestrator_root` arg
  with `$VCT_ORCHESTRATOR_ROOT`.

---

### 5. Gap 8 — Manifest doesn't track preserved files

**Severity**: foundation for future audit/diff work. **Effort**:
~50 LOC + manifest schema bump.

`.vco-manifest.json` tracks the 114 files VCO installed but NOT the
files VCO chose to preserve. After deferral resolution the only
record of "VCO once tried to install file X here" lives in the
deleted deferral file.

**Fix**: extend `.vco-manifest.json` schema with a
`preserved_files: {<rel_path>: {shipped_sha256, preserved_at,
shipped_source}}` section. Update on every install run.

Schema-bump compatibility: existing manifests lack this key. Reader
code should default to `{}` when absent — no migration needed.

**Code locations**:
- Manifest writer in `vco_lib/project_init.py` (find via grep on
  `vco-manifest.json` writes).
- Schema example update in any docs that reference manifest shape.

---

---

## 6. Gap 3 — OS-filter `.sh` vs `.ps1` at install time

**Severity**: real bug per user clarification 2026-05-13. **Effort**:
~40 LOC + 2 tests.

`.sh` and `.ps1` parity is required AT THE REPO LEVEL (CI gate). But
per-project install should select by host OS: install `.sh` only on
Linux/macOS, `.ps1` only on Windows. Currently both variants ship to
every project (~300 KB dead weight per the SD15 case, plus an
inflated preserved-file list when one of the variants is what's
preserved).

**Fix**: in the bundle install file-iteration helper, detect host OS
via `platform.system()` and skip the wrong-OS variant when copying.
The manifest should NOT record skipped-variant files (they're
"intentionally not installed for this OS", not "preserved").

**Edge case**: project moves between OSes later (e.g. clone to a
different host). The missing-variant files won't be there. Acceptable
— re-running `install-bundle` on the new host installs the right set.

**Code locations**:
- `vco_lib/project_init.py` — file-iteration in `install_bundle`.
- Manifest writer — skip records for OS-filtered variants.

**Tests**: patch `platform.system()` to return "Linux"/"Windows",
confirm the correct variants are filtered.

---

## 7. Observation 7 — CLAUDE.md / CONTEXT_STATE.md / MEMORY.md templates with placeholder substitution

**Severity**: real gap per user clarification 2026-05-13. **Effort**:
~120 LOC + 3 templates authored + 4 tests.

VCO doesn't currently ship templates for these 3 project-level files.
**Per the user, this is NOT intentional**: VCO should ship a "latest
reference version" of each + handle the install path.

**Fix**:

1. **Author templates** in `templates/`:
   - `templates/CLAUDE.md.template`
   - `templates/CONTEXT_STATE.md.template`
   - `templates/MEMORY.md.template`
   
   Use the orchestrator project's own files
   (`/home/martino/Desktop/PROGETTI/Claude/CLAUDE.md` etc.) as the
   reference for canonical structure, scrubbed of project-specific
   content, with placeholders like `{{PROJECT_NAME}}`,
   `{{PROJECT_ROOT}}`, `{{ORCHESTRATOR_ROOT}}` (same substitution
   pattern the existing agent files use).

2. **On install**:
   - **No pre-existing file**: write to the project's actual location
     (`<project>/CLAUDE.md` etc.) WITH placeholders substituted per
     project.
   - **Pre-existing file**: leave it alone (user-customized). BUT write
     the **unsubstituted raw template** to
     `<project>/.claude/context/templates/CLAUDE.md.reference` (and
     similarly for CONTEXT_STATE/MEMORY) so the project's Claude can
     review the existing file against canonical and decide what to
     update.
   - **Re-running install**: reference copies stay updated if the
     template in `VCO_dev/templates/` has changed.

3. **Why this is the narrow `.vco-shipped` sidecar (Obs 4) we DID
   want**: scoped to 3 high-value files instead of every file.
   Disk cost ≪ doubling `.claude/`.

**Placeholder substitution**: at least `{{PROJECT_NAME}}`,
`{{PROJECT_ROOT}}`, `{{ORCHESTRATOR_ROOT}}`. Substituted version goes
to the project file; unsubstituted (raw) version goes to the
reference path.

**Code locations**:
- New: `templates/CLAUDE.md.template`,
  `templates/CONTEXT_STATE.md.template`,
  `templates/MEMORY.md.template`.
- `vco_lib/project_init.py::install_bundle` — extend to handle these
  3 files with the pre-existence-aware install rule.

**Tests**:
- Project without any of the 3 files → all 3 created with substitution.
- Project with all 3 present → no overwrite; reference copies written
  under `.claude/context/templates/`.
- Re-running install with updated templates → reference copies update;
  project files untouched.
- Placeholder substitution correctness — every `{{KEY}}` becomes its
  intended value.

---

## Items DROPPED with rationale (don't ship)

| # | SD15 agent claim | Rationale for drop |
|---|---|---|
| Gap 1 | "Count is wrong (36 vs 42)" | Deferral is install-time snapshot, by design. Subsequent on-disk edits aren't reflected — that's the right semantics for "what VCO chose to preserve at install time". |
| Gap 4 | "No staleness classification" | The classification SD15 agent did took ~30 min of manual diffing. Automating it requires comparing file mtimes against orchestrator git history — yagni for a once-per-project port. |
| Gap 6 | "Session-noisy files not flagged" | Specific to pre-v0.2.0 hook threshold drift. Won't recur after this cycle. |
| Gap 9 | "No longitudinal install history" | Would require a separate audit log. The git history of `.claude/` already captures what changed when. Yagni. |
| Obs 4 | `.vco-shipped` sidecar files (universal) | Doubles `.claude/` disk footprint. Audit value real but cost too high for the marginal benefit on top of Gap 8 + Gap 11 fix. Narrow analog (3 high-value files) IS shipped via Obs 7. |

---

## Cross-cutting

- All 5 fixes ship together as one PR — they share a code surface
  (`vco_lib/project_init.py` + `vco_lib/deferral_report.py`).
- Version bump: TBD. If patches stay small, ship as v0.2.5; if any
  expands beyond scope, bump v0.3.0. Default: stick to v0.2.x.
- Cross-OS: same discipline as v0.2.2-v0.2.4. No `.sh` or `.ps1`
  affected by this work (pure Python).

## Manual test path

After implementation, replay the SD15 scenario:

1. Re-add an existing project with stale templates → bootstrap +
   bundle install runs → `UPDATE_DEFERRED.md` emitted + CLAUDE.md
   reminder block injected.
2. Open the project in a fresh Claude session → CLAUDE.md reminder is
   visible at session start.
3. Run `--update --force` to resolve → `UPDATE_DEFERRED.md` deleted
   + CLAUDE.md reminder block stripped + manifest's `preserved_files`
   section reflects post-resolution state.
4. Inspect the apply command in any test deferral → must contain
   `$VCT_ORCHESTRATOR_ROOT`, not a literal path.
5. With 30+ preserved files → inline list shows ~30, full list at
   `.claude/context/UPDATE_DEFERRED_files.txt`.

## Source documents

- SD15 agent's raw handoff (580 lines, includes full file-by-file
  forensics + suggested fixes per item):
  `.claude/context/sd15-install-handoff-2026-05-13.md`
- This plan (filtered to 5 real items):
  `.claude/context/plans/deferral-ux-polish-2026-05-13.md`
- Code reviewed: `vco_lib/project_init.py::install_bundle` +
  `vco_lib/deferral_report.py::DeferralReport` (read end-to-end
  before writing this plan).
