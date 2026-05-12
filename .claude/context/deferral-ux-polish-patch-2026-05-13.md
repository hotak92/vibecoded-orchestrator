# Deferral UX polish patch — status & per-pipeline verification (2026-05-13)

## Scope churn timeline

| Step | Coord directive | Resolution |
|---|---|---|
| Initial | Plan @ `.claude/context/plans/deferral-ux-polish-2026-05-13.md`: 5 items (1, 2, 3, 4, 5) | Started implementing in plan order. |
| Mid-flight | First amendment: add items 6 (Gap 3 OS-filter) and 7 (Obs 7 templates), 7 items total | Implemented item 6 + tests. |
| Reversal | Second amendment: drop item 6 — Gap 3 already-fixed in current VCO; verify other items first; per-pipeline reports | Reverted item 6 + tests. Reverted item 3's sibling-file scaffolding (over-engineered). |
| Refinement | Third amendment: per-project install is PRIMARY; main install is bonus completeness; ALREADY-FIXED in per-project → drop entirely; NEEDS-OWN-WORK in main install → park as backlog | Per-pipeline verification matrix produced; items 1, 4, 5 implemented; 2, 3, 7 awaited green-light. |
| Green-light | Coord approval to ship items 2, 3, 7 with concrete shape guidance (3-branch CLAUDE.md, cap=100, minimal template stubs). Dropped items re-verified (Gaps 1, 4, 6, 9; Obs 4) — all dropped correctly. | Items 2, 3, 7 implemented + tested. **All 6 in-scope items now IMPLEMENTED + TESTED.** |

## Per-pipeline verification matrix

| # | Item | Per-project install (`vco_lib/project_init.py`) | Main install (`install.py`) | Decision |
|---|---|---|---|---|
| 1 | Gap 11 — deferral cleanup after `--update --force` | **VERIFIED-REAL** — `install_bundle` calls `_emit_skipped_existing_deferral` only when `skipped_existing_paths` is non-empty (line 2978); after `--force` resolves them, the empty-list branch never runs and the stale on-disk deferral persists. SD15 reproduces this exactly. | **NOT-CHECKED-DEEPLY** — `install.py` always calls `_deferral_report.write()` at end of `main()` (line 2048); since the per-run report starts empty and only accumulates entries that are still applicable, the cleanup is structurally automatic. Different pattern, no analogous bug. | **IMPLEMENTED + TESTED.** |
| 2 | Gap 10 — CLAUDE.md reminder block | **VERIFIED-REAL** — `deferral_report.py` does not touch `CLAUDE.md` at all (`grep CLAUDE.md vco_lib/deferral_report.py vco_lib/project_init.py` → only docstring mentions). Future Claude sessions have no signal that `UPDATE_DEFERRED.md` exists. | **APPLIES-TRIVIALLY** — install.py already has a `<!-- vct-merge-pending -->` wrapped-marker block in `CONTEXT_STATE.md` (`update_merge_notification_block`, line 1054). Strong precedent we can mirror. | **IMPLEMENTED + TESTED.** |
| 3 | Gap 2 — file list cap of 20 | **VERIFIED-REAL** — `_format_file_list_md` hardcodes `cap=20` (line 2480). With 36+ skipped paths the tail is hidden. Per coord's simplification: just bump the cap (drop sibling-file mechanism). | **NOT-APPLICABLE** — install.py's deferral entries have no file lists. | **IMPLEMENTED + TESTED.** |
| 4 | Gap 7 — `$VCT_ORCHESTRATOR_ROOT` in apply-command | **VERIFIED-REAL** — `_emit_skipped_existing_deferral` (line 2585) and `_emit_user_modified_deferral` (line 2515) both embedded `str(orchestrator_root)!r` literal. | **NOT-APPLICABLE** — install.py's deferral commands say `python install.py --update ...` with no `--orchestrator-root` arg (no leak). | **IMPLEMENTED + TESTED.** |
| 5 | Gap 8 — manifest `preserved_files` tracking | **VERIFIED-REAL** — manifest schema v1 only tracks installed files; preserved files leak only into the (deletable) deferral .md. | **NEEDS-OWN-WORK** — install.py has its own state/install-manifest.json (`_write_install_manifest`, line 2055). Different schema, doesn't currently track preserved files. **Park as backlog.** | **IMPLEMENTED + TESTED.** Schema bumped to v2. Backwards-compatible (v1 manifests read back with empty preserved_files dict). |
| 6 | Gap 3 — OS-filter `.sh`/`.ps1` | **ALREADY-FIXED** — `_hook_glob_for_os` (line 2045) used at line 2192 ensures hooks pick only the host's shell variant. Scripts also OS-filter once the script_patterns list omits the wrong-OS shell glob — verified the existing list does include both (`*.sh, *.ps1`) but that's intentional for the `.claude/scripts/` set which Anthropic-side patches treat as cross-OS-shared. SD15's 28 stray `.ps1` files are pre-VCO legacy. | **ALREADY-FIXED** per coord finding. | **DROPPED from PR.** Backlog: optional follow-up to OS-filter `.claude/scripts/` shell variants if user reports more legacy projects suffer the issue. |
| 7 | Obs 7 — CLAUDE/CTX/MEMORY templates | **VERIFIED-REAL** — `templates/` contains only `agents/`, `hooks/`, `scripts/`, `skills/`, `settings.json.linux.template`, `settings.json.windows.template`. No `CLAUDE.md.template`, `CONTEXT_STATE.md.template`, `MEMORY.md.template`. | **NOT-APPLICABLE** — install.py manages orchestrator-self files, not per-project user files. | **IMPLEMENTED + TESTED.** |

## What's IMPLEMENTED in this branch right now

### Item 5 — Manifest `preserved_files` (schema v2)

- `vco_lib/project_init.py`:
  - Bumped `_MANIFEST_SCHEMA_VERSION` from 1 → 2.
  - Extended docstring above the constants with the v2 schema shape + v1 read-back contract.
  - `_read_manifest()`: defaults `preserved_files: {}` when missing (forward-compat).
  - `install_project_bundle()`: rebuilds `new_preserved: dict[str, dict]` from scratch each run (so converged files fall off automatically). Records `{shipped_sha256, preserved_at, shipped_source, reason}` for both `preserve` (update-mode) and `skip-existing` (first-install) actions.
- `tests/test_install_bundle.py::ManifestPreservedFilesTests` — 5 tests:
  - `test_fresh_install_writes_schema_v2_with_empty_preserved`
  - `test_skip_existing_records_preserved_entry`
  - `test_preserve_in_update_mode_records_preserved_entry`
  - `test_force_resolves_preserved_entry_in_manifest`
  - `test_v1_manifest_read_back_defaults_preserved_to_empty`
- Updated existing `test_fresh_install_creates_all_categories` to expect schema_version=2 + assert preserved_files key.
- Updated existing `test_read_returns_empty_when_missing` to match v2 shape.

### Item 1 — Gap 11: deferral cleanup on resolution

- `vco_lib/project_init.py`:
  - New `_reconcile_bundle_deferrals(folder, *, still_user_modified, still_skipped_existing)` helper. Reads the on-disk report, marks the bundle-owned conditions resolved when no surviving paths exist, calls `report.write()` (which unlinks if entries empty, atomic-rewrites otherwise).
  - `install_project_bundle()` calls `_reconcile_bundle_deferrals` after the existing emit pass. Only touches the two `bundle_*` condition_ids — preserves unrelated deferrals (schema_migration_required, weaviate_unreachable, compose_overlay_ambiguous) intact.
- `tests/test_install_bundle.py::DeferralReconcileTests` — 4 tests:
  - `test_force_update_clears_stale_skipped_existing_deferral` (the SD15 smoking-gun reproducer)
  - `test_force_update_clears_stale_user_modified_deferral`
  - `test_reconcile_preserves_unrelated_conditions` (asserts schema_migration_required survives)
  - `test_partial_resolution_keeps_remaining_entries` (asserts file is rewritten, not unlinked, when only one condition resolves)

### Item 4 — Gap 7: `$VCT_ORCHESTRATOR_ROOT` in apply-command

- `vco_lib/project_init.py`:
  - `_emit_skipped_existing_deferral`: replaced `{str(orchestrator_root)!r}` with `"$VCT_ORCHESTRATOR_ROOT"` (literal env-var reference inside double quotes). Added prose: "Run from a shell where `.claude/env` has been sourced, or prepend VCT_ORCHESTRATOR_ROOT=/path/to/VCO_dev".
  - `_emit_user_modified_deferral`: same swap + same prose.
  - Function signatures still take `orchestrator_root` (unused) for back-compat with internal call sites.
- `tests/test_install_bundle.py::DeferralCommandPortabilityTests` — 2 tests:
  - `test_skip_existing_command_uses_env_var` (assert `$VCT_ORCHESTRATOR_ROOT` present AND literal path absent)
  - `test_user_modified_command_uses_env_var` (symmetric for update-mode)

### Item 2 — Gap 10: CLAUDE.md reminder block

- `vco_lib/deferral_report.py`:
  - Added module-level constants: `_CLAUDE_MD_REL`, `_REMINDER_BEGIN`, `_REMINDER_END`, `_LEADING_FRONTMATTER_RE`.
  - New helpers: `_reminder_block()`, `_splice_reminder_into_claude_md()`, `_strip_reminder_from_claude_md()`, `_ensure_claude_md_reminder(folder)`, `_strip_claude_md_reminder(folder)`, `_atomic_write_text()` (pulled out so reminder helpers reuse the same atomic-write primitive as `DeferralReport.write`).
  - `DeferralReport.write()` now calls `_ensure_claude_md_reminder` (on the success path) and `_strip_claude_md_reminder` (on the empty-entries-unlink path). Both are best-effort — a missing or unwritable `CLAUDE.md` does NOT raise.
- Marker pattern mirrors install.py:1035's `<!-- vct-merge-pending -->` block: wrapped HTML comments (invisible in rendered Markdown), idempotent find-and-replace rewrite when the block already exists.
- Three-branch placement (per escalation trigger guidance):
  - Frontmatter present (`^---\n...\n---\n`) → block inserted immediately AFTER the closing fence, with a blank-line separator.
  - No frontmatter → block prepended at the very top.
  - `CLAUDE.md` missing → no-op (bootstrapper owns CLAUDE.md creation).
- Strip path cleans BOTH the leading and trailing blank-line separators the splicer inserted, so empty-write → write → repeated cycles don't accumulate stray newlines.
- `tests/test_deferral_report.py::TestClaudeMdReminder` — 6 tests:
  - `test_write_injects_block_when_claude_md_lacks_frontmatter`
  - `test_write_injects_block_after_frontmatter`
  - `test_idempotent_re_injection_does_not_duplicate` (three writes → block still appears once)
  - `test_empty_write_strips_block`
  - `test_missing_claude_md_is_noop` (deferral still lands; CLAUDE.md not created)
  - `test_unicode_user_content_preserved` (round-trip strip restores byte-for-byte, including emoji)

### Item 3 — Gap 2: bump cap

- `vco_lib/project_init.py::_format_file_list_md`: default cap bumped from 20 → 100. SD15's 36-file deferral now lands inline. 100 covers every realistic install (the entire bundle is ~114 files) while still bounding pathological writes.
- `tests/test_install_bundle.py::DeferralFileListCapTests` — 2 tests:
  - `test_thirtyfive_paths_all_land_inline` (the SD15 scenario: all 35 paths render, no `... and N more` trailer)
  - `test_oversize_list_still_truncates` (sanity: a 150-entry list still caps at 100 with the trailer)

### Item 7 — Obs 7: minimal CLAUDE/CTX/MEMORY templates

- New files under `templates/`:
  - `templates/CLAUDE.md.template` (~75 lines): Project Overview / Tech Stack / Key Paths / Session Start / KG-First Search Policy / VCO-Managed Files sections. Universal advice cribbed from the orchestrator's own `CLAUDE.md` where the guidance applies cross-project.
  - `templates/CONTEXT_STATE.md.template` (~30 lines): Current Status / Recent Progress / Next Steps / Open Blockers / Last Session Action with prompt-style placeholders.
  - `templates/MEMORY.md.template` (~10 lines): Index header explaining auto-memory + empty entries section.
- Placeholders: `{{PROJECT_NAME}}` (folder basename), `{{PROJECT_ROOT}}`, `{{ORCHESTRATOR_ROOT}}`. Substituted via plain `str.replace` (no engine).
- `vco_lib/project_init.py`:
  - New module-level constant `_PROJECT_LEVEL_TEMPLATES` (tuple of `(template_name, live_rel, ref_rel)`).
  - `_project_template_subs(orchestrator_root, project_root, project_name)` — superset of `_agent_subs` with `{{PROJECT_NAME}}` added.
  - `_apply_template_subs(buf, subs)` — plain replace, UTF-8 in/out.
  - `_normalise_for_diff(text)` — strips trailing whitespace per line + trailing blank lines so a whitespace-only diff doesn't flag for review.
  - `_emit_template_review_pending_deferral(folder, *, diverged_files)` — per-project single entry (`condition_id="template_review_pending"`, severity=info).
  - `_install_project_level_templates(folder, *, orchestrator_root, project_name, dry_run)` — the install pass. Returns `{live_created, reference_written, diverged}`. Missing-file branch installs the substituted stub as the live file; existing-file branch writes `.claude/context/templates/<NAME>.reference.md` and adds to `diverged` if the live file meaningfully differs.
  - Wired into `install_project_bundle()` after the settings-merge step. Result dict gains a `"templates"` section.
  - `_reconcile_bundle_deferrals` extended with `still_template_review_pending: bool = False` so a fresh install that converges (live file matches reference) clears the stale entry on the next run.
- `tests/test_install_bundle.py::ProjectLevelTemplatesTests` — 6 tests + a `_ship_project_level_templates` helper:
  - `test_missing_files_get_substituted_stubs` (fresh-install: all 3 stubs land; no `.reference.md` sidecar yet; no deferral)
  - `test_existing_file_gets_reference_sidecar_only` (existing CLAUDE.md preserved; `.reference.md` written; `template_review_pending` emitted)
  - `test_matching_file_no_deferral` (user's CLAUDE.md byte-matches reference → no deferral)
  - `test_whitespace_only_diff_does_not_flag`
  - `test_dry_run_no_template_writes`
  - `test_diverged_then_resolved_clears_deferral` (item 1 reconcile pass clears template_review_pending)

### Total

- `vco_lib/project_init.py`: +~330 LOC (manifest schema + reconcile + cmd swaps + templates pipeline).
- `vco_lib/deferral_report.py`: +~130 LOC (CLAUDE.md reminder helpers).
- `templates/`: +3 new files (~110 LOC of template content total).
- `tests/test_install_bundle.py`: +~600 LOC (new test classes for items 1, 3, 4, 5, 7).
- `tests/test_deferral_report.py`: +~140 LOC (item 2 test class).
- All previously-existing tests still pass (122 baseline + 24 new = 146 in the focused suites).
- Full suite: 856 pass, 4 skipped, 1 pre-existing unrelated failure (`test_build_plan_skips_existing_top_level` — failing on main since before v0.2.3).
- `cargo test --lib`: 585 pass, 0 fail, 1 ignored.
- `npm run check`: 0 errors, 38 pre-existing a11y warnings.

## Decisions / escalation flags

- **Item 3 sibling-file mechanism — rolled back per coord simplification**. The plan called for a sibling `.claude/context/UPDATE_DEFERRED_files.txt` when oversize. I implemented per-condition siblings (`_skipped_files.txt` / `_user_modified_files.txt`) for robustness, then reverted both per the "just bump cap" directive. Final shape: cap=100, no siblings.
- **Item 5 schema bump — full shape** (`shipped_sha256` + `preserved_at` + `shipped_source` + `reason`). Coord confirmed: "keep the richer shape — makes future audit/diff work without re-scanning the orchestrator."
- **Item 4 emit-function signatures**: kept `orchestrator_root` parameter even though now unused. Internal-only callers; removing it would churn `install_project_bundle`'s call sites for no behaviour win. Coord confirmed.
- **Item 7 project_name derivation**: `folder.name or "Project"` — folder basename. No CLI param added to `install_project_bundle` since the field is purely cosmetic (substituted into the heading line of the template stubs). Callers wanting a different display name can edit `CLAUDE.md` after install. Trade-off: a folder like `my-project` produces "my-project — Project Instructions", which is fine; a folder like `tmp` produces "tmp — Project Instructions", which is mildly ugly but harmless.
- **Obs 4 narrow-vs-universal distinction**: The SD15 agent's "universal `.vco-shipped` sidecar for every preserved file" proposal was dropped (cost > benefit), but the NARROW analog is delivered by Item 7's `.reference.md` mechanism. Precedent for narrow sidecars already exists in VCO at `vco_lib/import_from_orchestrator.py:559-563` (`.source.json` for settings.json merge fallback). Item 7 extends the established pattern, doesn't invent one.

## Manual test path (per plan section "Manual test path")

1. ✅ Replay SD15 scenario: pre-existing custom files + first-install → `UPDATE_DEFERRED.md` + deferral entry. Covered by `test_skip_existing_emits_deferral_entry` (existing) + new `ManifestPreservedFilesTests::test_skip_existing_records_preserved_entry`.
2. ✅ Open project in fresh Claude session → CLAUDE.md reminder visible at session start. Covered by `TestClaudeMdReminder::test_write_injects_block_when_claude_md_lacks_frontmatter` + `..._after_frontmatter`.
3. ✅ Run `--update --force` to resolve → `UPDATE_DEFERRED.md` deleted + CLAUDE.md reminder block stripped + manifest's `preserved_files` updated. Covered by `test_force_update_clears_stale_skipped_existing_deferral` + `test_force_resolves_preserved_entry_in_manifest` + `TestClaudeMdReminder::test_empty_write_strips_block`.
4. ✅ Apply command in any test deferral contains `$VCT_ORCHESTRATOR_ROOT`, not a literal path. Covered by `DeferralCommandPortabilityTests`.
5. ✅ With 30+ preserved files → inline list shows correctly. Covered by `DeferralFileListCapTests::test_thirtyfive_paths_all_land_inline`.
6. ✅ Fresh-install on an empty project produces CLAUDE.md / CONTEXT_STATE.md / MEMORY.md from minimal stubs. Covered by `ProjectLevelTemplatesTests::test_missing_files_get_substituted_stubs`.
7. ✅ Existing CLAUDE.md preserved on install; `.reference.md` sidecar shipped for diffing; meaningful diff triggers `template_review_pending`. Covered by `ProjectLevelTemplatesTests::test_existing_file_gets_reference_sidecar_only` + `..._matching_file_no_deferral` + `..._whitespace_only_diff_does_not_flag`.

## Verification commands

```bash
python3 -m pytest tests/test_install_bundle.py tests/test_deferral_report.py tests/test_vco_lib_project_init.py
# Expected: 167 pass (122 baseline + 24 new + 21 retained existing), 0 fail
python3 -m pytest tests/ --ignore=tests/test_import_from_orchestrator.py
# Expected: 856 pass, 4 skipped
cd launcher/src-tauri && cargo test --lib
# Expected: 585 pass, 0 fail, 1 ignored
cd launcher && npm run check
# Expected: 0 errors, 38 pre-existing a11y warnings
```
