# VCO Install — Findings & Gaps Discovered in SD15

**Audience**: VCO_dev's agent (the team maintaining the orchestrator).
**Date**: 2026-05-13
**SD15 state at writing**: VCO install completed 2026-05-12T18:17:21Z. 42 files
preserved (differ from shipped). User invoked Claude in SD15 with the prompt
"check what's left to set up" and the findings below emerged.

---

## What the existing deferred file (`UPDATE_DEFERRED.md`) DID surface

The auto-generated `.claude/context/UPDATE_DEFERRED.md` contains:
- One `bundle_skipped_existing_files` deferral entry, severity `info`.
- A truncated file list: 20 paths visible, `... and 16 more` trailer (claimed total: 36).
- A single command-to-apply (`install-bundle --update --force`).
- The orchestrator path (`/home/martino/Desktop/PROGETTI/VCO_dev`) embedded in the command.

That's it. Everything in the following section is **NOT** in the deferral file and
had to be reconstructed via forensic comparison between
`VCO_dev/templates/` and `SD15/.claude/`.

---

## Gap analysis — what's missing from the deferral file

### Gap 1: The reported count is wrong

The deferral lists 36 files. The actual count of files where SD15 diverges from
VCO's shipped versions is **42**. The 6 extra are files that drifted after the
deferral was captured (the deferral is a frozen snapshot of install time;
subsequent edits aren't reflected).

The user has no signal that the deferral is stale. Re-running the install would
re-emit a corrected deferral but most users won't think to do that.

### Gap 2: The list is truncated

`_format_file_list_md(paths, cap=20)` in `vco_lib/project_init.py:2455` caps at
20 entries. With 36+ files in our case, the trailing 16+ are hidden behind
`... and 16 more` with no way for the user to expand without manually re-running
the diff.

### Gap 3: 28 PowerShell hooks were silently installed but never mentioned

VCO installed 28 `.ps1` files (PowerShell variants of the hooks) into
`.claude/hooks/` even though SD15 is on Linux. These never fire, never run,
contribute ~300 KB of dead weight. The deferral doesn't acknowledge them at all
(no entry like "PowerShell variants installed on non-Windows host —
ignore safely").

### Gap 4: No staleness classification

The deferred file presents all 36 files uniformly. In reality they span a wide
staleness range:

| Bucket | Files | SD15 age | Indicator |
|---|---|---|---|
| Very stale (3+ months old) | 25 | 92–115 days | Outdated patterns, missing recent bug fixes |
| Moderately stale (1–2 months) | 5 | 28–62 days | Refactored elsewhere |
| Recently shipped, locally outdated (7 days) | 7 | 0.2 days | Trivial template substitution (`{{ORCHESTRATOR_ROOT}}` vs hardcoded path) |
| Newly ineligible (PS1 hooks) | not in deferral | n/a | Wrong OS |

A user can't tell which files matter most to update without diffing each one
by hand.

### Gap 5: No diff content / no intent classification

The deferral says "files differ", not "here's how". The user can't tell, from
the deferral alone:
- Are these stale templates? → almost always YES in our case
- Or intentional user customizations? → NONE in our case, but we had to check
- Or local mutations by tooling? → NONE in our case
- What's the actual diff content? → not surfaced

Classifying these required reading each file in both locations and looking for:
- Hardcoded project names that don't match (e.g. `"ARTup"` in
  `code-graph-updater.md` — clearly a leftover from a template that originated
  in a different project)
- Files where the "user version" predates the bundle install date
- Files where the "user version" lacks structural features VCO introduced
  later (e.g. YAML frontmatter, `set -euo pipefail`, env-var scrubbing)

### Gap 6: No identification of session-noisy stale files

`.claude/hooks/context-size-check.sh` fires on every session start with the OLD
200-line threshold. VCO ships it with 400. So every session warns about
CONTEXT_STATE.md being over budget (669 lines) when actually it's only 1.67x
the modern threshold, not 3.3x. The deferral doesn't flag this hook as
"high-priority to update because it's spamming your session output". The user
discovered this by reading the hook code, not by reading the deferral.

### Gap 7: No record of what would have been installed

The `.vco-manifest.json` tracks the 114 files VCO actually installed (sha256 +
template-source path). It does NOT record the files VCO chose to PRESERVE. So
the only place that information lives is in the deferral file, which gets
deleted when resolved. After a `--force` run the project loses all record of
"which 42 files VCO once decided to preserve here".

### Gap 8: External path dependency

The deferral's `command_to_apply` embeds `/home/martino/Desktop/PROGETTI/VCO_dev`.
This is:
- Specific to the machine where the install was run (won't survive a project sync).
- Specific to the user's local clone (won't work if VCO_dev moves).
- Stable only as long as the user doesn't touch their VCO_dev clone.

A user opening SD15 on a different machine has no way to apply the deferral
without first re-cloning VCO_dev at the same path.

### Gap 9: No "what changed" history

The deferral file is generated at install time and cleared on resolution. There's
no longitudinal record like "VCO has tried to update this file N times across M
install runs; each time you preserved it". Useful for spotting files where the
user really has diverged on purpose vs files that are reliably stale.

### Gap 10: CLAUDE.md / CONTEXT_STATE.md don't reference the deferral file

When VCO emits a deferral, it writes `.claude/context/UPDATE_DEFERRED.md` and
... stops. Nothing in the project tells future Claude sessions to actually
**read** that file at session start.

In practice this means:
- A user opens SD15 in Claude weeks after a VCO install.
- Claude's SESSION-START checklist (in CLAUDE.md) tells it to read
  CONTEXT_STATE.md, search the KG, etc.
- Nothing mentions `UPDATE_DEFERRED.md`.
- The user only discovers it exists by happening to ask "is anything
  pending from VCO?"

**Suggested fix**: When VCO writes a deferral, it should also **idempotently
update the project's CLAUDE.md** (or a top section of it) with a wrapped
SESSION-START reminder + clear cleanup instructions:

```markdown
<!-- vco-deferral-reminder-begin -->
**Pending VCO action:** `.claude/context/UPDATE_DEFERRED.md` exists.
Read it at session start; it contains commands to resolve unresolved
VCO install actions.

To remove THIS reminder block: once the deferral is resolved (either
via the apply command in UPDATE_DEFERRED.md or via
`python -m vco_lib.project_init dismiss-deferral`), VCO will delete
UPDATE_DEFERRED.md AND strip everything between the vco-deferral-reminder
markers from CLAUDE.md. If VCO didn't clean up properly, the user (or
Claude) can manually delete this block.
<!-- vco-deferral-reminder-end -->
```

Key behaviors the implementation must guarantee:

1. **Wrapped in `<!-- vco-deferral-reminder-begin -->` /
   `<!-- vco-deferral-reminder-end -->`** so VCO can find and remove it
   programmatically.
2. **Idempotent**: re-running an install with the same deferral doesn't
   duplicate the block; just updates its content.
3. **Self-cleaning**: when `UPDATE_DEFERRED.md` is deleted (deferral
   resolved), VCO's next run (or `dismiss-deferral`) MUST strip the
   reminder block from CLAUDE.md too. The block itself documents how
   to clean it up manually as a fallback.
4. **Includes the cleanup recipe IN the block** so future Claude sessions
   that resolve the deferral know to remove the reminder, not leave it
   stale referencing a non-existent file.
5. Optionally, also write to CONTEXT_STATE.md (since most users read that
   first anyway). Same wrapped-marker pattern.

Same idea applies to other deferral types:
- `schema_migration_required` — Claude needs to know there's a pending
  Weaviate change before consuming the KG.
- `weaviate_unreachable_at_bootstrap` — Claude should know "the KG
  might be partial / stale right now".

**Code location**: `vco_lib/project_init.py` — at the end of each
`_emit_*_deferral` function, also call a new
`_ensure_claude_md_reminder(folder, deferral_id, cleanup_instructions)`
helper. The corresponding cleanup happens in `DeferralReport.write` in
`vco_lib/deferral_report.py` when it detects the report is empty and
deletes the deferral file: at that moment, also call
`_remove_claude_md_reminder(folder, deferral_id)`.

---

## TL;DR for VCO_dev maintainers

1. The **deferral file (`UPDATE_DEFERRED.md`) is the right idea**, but the project
   folder should be **self-sufficient** for resolving the deferral. Currently it
   references the user's local launcher folder (`/home/martino/Desktop/PROGETTI/VCO_dev`),
   which:
   - Is a path that lives outside the project tree
   - Will be specific to each user's machine
   - Means the user can't inspect "what would VCO replace this file with" without
     leaving the project folder
2. The preserved-file list in `UPDATE_DEFERRED.md` **caps at 20 entries** (`... and N
   more`). When the actual list is 36 (or in our case 42), the user can't see most
   entries without running `--apply-deferred` or manually diffing.
3. The deferral surfaces **only the file paths, not the nature of the differences**.
   Categorizing "stale template scaffold" vs "intentional user customization"
   currently requires a manual diff loop.
4. There's no concept of **what VCO would have shipped** preserved in the project
   itself. The manifest stores sha256 only; the actual shipped content lives
   externally in `VCO_dev/templates/`. A user with no `VCO_dev/` clone (or a user
   whose orchestrator clone has been updated since their last `--update`) has no
   way to see what their preserved files would have been replaced by.

---

## Detailed observations

### Observation 1: External path leak in `UPDATE_DEFERRED.md`

The auto-generated `apply` command embeds the user's local launcher path:

```bash
python -m vco_lib.project_init install-bundle \
  --folder '/home/martino/Desktop/PROGETTI/SD15' \
  --orchestrator-root '/home/martino/Desktop/PROGETTI/VCO_dev' \  # ← LEAKED
  --update --force --json
```

**Problem**: `/home/martino/Desktop/PROGETTI/VCO_dev` is a path baked at install
time that's not portable. If the user moves the orchestrator, archives it,
re-clones it under a different path, or syncs the project to another machine,
this command breaks.

**Suggested fix**: The deferral writer should reference an env var
(`$VCT_ORCHESTRATOR_ROOT`, which is already set in `.claude/env`) OR ship the
shipped-versions of the preserved files INSIDE the project folder (see
Observation 4 for that approach).

**Code location**: `vco_lib/project_init.py:2554` — `_emit_skipped_existing_deferral`
hardcodes the orchestrator_root into the command string.

### Observation 2: Truncation hides most of the deferral content

```python
def _format_file_list_md(paths: list[str], cap: int = 20) -> str:
```
(`vco_lib/project_init.py:2455`)

With 42 preserved files, the user sees:
- 20 paths
- `... and 22 more`

The user has no way to see which 22 are hidden without running
`--apply-deferred --dry-run` or rewriting the deferral writer themselves.

**Suggested fix options**:
- Lift the cap when the deferral is the only one (no risk of unbounded file
  growth — total count is bounded by the bundle size).
- Write the full list to a sibling file (e.g.
  `.claude/context/UPDATE_DEFERRED_files.txt`) and reference it.
- Make the cap configurable (e.g. via `--list-cap` flag or env var).

### Observation 3: Missing diff/intent classification

Each deferred file falls into one of three categories:
1. **Stale template scaffold** (most common) — was an early-version template
   shipped before the file got refactored upstream. Should accept VCO's
   newer version.
2. **Pre-existing user customization** — the user intentionally diverged from
   the template (e.g. project-specific hook content).
3. **Local mutation by tooling** — the file was mutated by some agent or hook
   without the user noticing.

The current deferral entry treats all three identically and tells the user
"run `--force` to overwrite". A user with category 2 files will silently lose
their customizations.

**Empirical data from SD15**: All 42 preserved files were category 1 (stale
templates). Indicators we used to classify:
- Files with hardcoded project names that don't match SD15 (e.g. "ARTup" in
  `code-graph-updater.md`)
- Files predating SD15's first install (mtime older than the bundle install
  date by months)
- Files where the "user version" is missing the YAML frontmatter that VCO
  added in a later refactor
- Files where the "user version" lacks bug-fix commits visible in VCO's git log
  (e.g. `code-graph-analyze` venv-detection fixes dated 2026-04-28 and
  2026-05-07, both present in VCO but absent from SD15)

**Suggested fix**: At install time, when a file is being preserved, emit a
*diff summary* into the deferral entry:
- Bytes added/removed
- Number of changed lines
- Whether the preserved version is older or newer than the shipped one
- Whether the preserved version has the expected YAML frontmatter

This lets the user (or a future agent) quickly triage which files are likely
stale vs which are likely customized.

### Observation 4: No in-project copy of what-would-have-shipped

If the user's `VCO_dev/` clone evolves (which it will), the deferral command
becomes a moving target. A user re-running `--update --force` next month gets
a different result than running it today.

**Suggested fix**: When VCO preserves a file, ALSO write a `.vco-shipped`
sidecar file alongside it. E.g. when preserving `.claude/hooks/pre-tool-use.sh`,
write `.claude/hooks/pre-tool-use.sh.vco-shipped` containing what VCO would
have installed at that point. This way:
- The project becomes self-sufficient for resolving the deferral
- A user can `diff pre-tool-use.sh pre-tool-use.sh.vco-shipped` in-tree
- The deferral command can reference local files instead of external paths
- Time-travel is preserved (the sidecar reflects the orchestrator state at the
  install time the deferral was emitted, not the orchestrator's current state)
- Re-running `--update` cleans up sidecars when the file is overwritten

Implementation outline: see `_emit_skipped_existing_deferral` in
`vco_lib/project_init.py:2529`. Also need the install logic that decides to
preserve a file (likely in `import_from_orchestrator.py`) to additionally
write the sidecar.

### Observation 5: PS1 hooks installed silently on Linux

VCO installed 28 PowerShell `.ps1` hooks alongside the `.sh` ones. On a Linux
project these never fire. Not a bug — just dead weight (~300 KB).

**Suggested fix**: Detect OS at install time and skip the irrelevant variants,
or store the irrelevant variants in a `disabled/` subfolder so the user can
see them without them being in the active hook list.

### Observation 6: `context-size-check.sh` threshold mismatch

VCO ships this hook with `MAX_LINES=400 / WARN_LINES=300`. SD15's preserved
copy has `MAX_LINES=200 / WARN_LINES=150` — the older lower threshold.

The hook currently fires every session start because CONTEXT_STATE.md is
669 lines (over the OLD threshold of 200), even though VCO's newer threshold
(400) would have only warned about the genuine "way too big" condition.

This is a self-resolving issue after `--update --force`, but worth flagging
because **the noisy hook output during session start probably wastes context
tokens for users in this exact situation**. The deferral system should
prioritize updating files known to spam session output (hooks especially).

**Suggested fix**: Tag certain shipped files as "session-noisy when stale"
in the templates dir (or in vco_lib code). When preserved, surface them at
the top of the deferral with a "highly recommended to update" marker.

### Observation 7: VCO doesn't ship `CONTEXT_STATE.md.template` / `MEMORY.md.template` / `CLAUDE.md.template`

The user mentioned "VCO usually leaves a 'duplicate' template for how to update
these files" — but my forensics show VCO doesn't actually ship templates for
these three project-level files. VCO_dev's own `CLAUDE.md` serves as the
**reference example** that users are expected to read and adapt.

**Suggested fix**: Either:
1. Add `.template` versions of these three files to `VCO_dev/templates/` so
   they're available in every installed project. Useful as a starting point
   for fresh projects.
2. Document explicitly that VCO_dev's own `CLAUDE.md` IS the template, and
   include a "create your CLAUDE.md from this template" prompt in the BOOTSTRAP
   flow.
3. Add an install-time check: if the target project has no CLAUDE.md, write a
   minimal stub (using VCO_dev's as a starting point) and emit a deferral
   saying "review and adapt".

The current state means new projects either inherit a CLAUDE.md from
project-bootstrapper (if used) or have none until a human writes one.

### Observation 8: Manifest doesn't track preserved files

`.vco-manifest.json` tracks the 114 files VCO installed (sha256 + source). It
does NOT track the files VCO chose to PRESERVE — those exist as a one-shot
mention in the deferral entry that gets cleared when the deferral is dismissed.

So six months from now, when the user looks at SD15 and asks "did VCO ever try
to install file X here?", there's no record. They'd have to look up old
deferral entries (which are deleted when resolved).

**Suggested fix**: Add a `preserved_files` section to `.vco-manifest.json`:
```json
{
  "files": { ... installed ... },
  "preserved_files": {
    ".claude/hooks/post-tool-security.sh": {
      "shipped_sha256": "abc...",
      "preserved_at": "2026-05-12T18:17:21Z",
      "shipped_source": "templates/hooks/post-tool-security.sh",
      "still_diverged": true | false  // auto-updated each install run
    }
  }
}
```

This way:
- A future install can detect if the user's version converged with the shipped
  version (deferral can auto-clear).
- The user has a permanent record of every file the orchestrator chose not to
  overwrite.
- Combined with Observation 4 (sidecar files), provides total self-sufficiency.

---

## Concrete file-by-file analysis (SD15 as of 2026-05-13)

All 42 preserved files were classified as **stale template scaffold** by the
analyses above. No file was found to contain user-intent customizations.

### Most-stale (would benefit most from `--update --force`)

| File | SD15 age (days) | VCO age (days) | Reason for update |
|---|---|---|---|
| `.claude/hooks/context-size-check.sh` | 115 | 4 | Threshold mismatch (200 vs 400), missing env scrubbing, missing `_lib/stderr-cap.sh` sourcing |
| `.claude/skills/*` (8 SKILL.md files) | 103 | 7 | Schema/format update from VCO |
| `.claude/scripts/*` (16 files) | 92 | 3-7 | Recent venv-detection bug fixes (2026-04-28, 2026-05-07) |
| `.claude/agents/code-graph-updater.md` | 62 | 7 | Missing YAML frontmatter; long-form knowledge-system docs refactored to a single reference link |
| `.claude/agents/knowledge-curator.md` | 62 | 7 | Same as above |
| `.claude/agents/graph-health-checker.md` | 62 | 7 | Same as above |
| `.claude/hooks/pre-tool-use.sh` | 35 | 3 | Doubled in size — likely large bug fixes / new features |
| 7 agent .md files (planner, expert-coder, coder, ai-agentic-architect, project-coordinator, project-architect, tester) | 0.2 | 7 | Identical -51 byte diff: VCO templates use `{{ORCHESTRATOR_ROOT}}` placeholder, our locals have a hardcoded path baked in |

### Action taken in SD15 (next step in this session)

User has authorized `--update --force`. About to run it to bring all 42 files
to VCO's latest shipped versions.

---

## What would have been ideal

If VCO had implemented Observations 4 + 8 (sidecar files + manifest tracking
of preserved files), this entire investigation would have been a single shell
command in the SD15 project root:

```bash
# In SD15, see exactly what VCO would have changed:
for f in $(jq -r '.preserved_files | keys[]' .claude/.vco-manifest.json); do
  echo "=== $f ==="
  diff "$f" "$f.vco-shipped"
done

# Then choose: keep local, take shipped, or merge.
```

No reference to `VCO_dev/`. No external paths. No `--apply-deferred --dry-run`
ceremony. Self-contained, version-stable, machine-portable.

---

## Suggested priority order for VCO_dev fixes

1. **CLAUDE.md / CONTEXT_STATE.md auto-injection of deferral reminders** (Gap 10) —
   single biggest UX win. Without it, deferrals are invisible to future
   Claude sessions unless the user remembers to ask. Cheap to implement
   (wrapped-marker block, idempotent write, self-cleaning).
2. **Sidecar `.vco-shipped` files** (Observation 4) — makes the project
   self-sufficient for resolving deferrals without leaving the project tree.
3. **Manifest `preserved_files` tracking** (Observation 8) — pairs with #2,
   gives a permanent audit trail.
4. **Diff summary in deferral entries** (Observation 3) — helps users triage
   without diff/manual.
5. **Path portability in deferral commands** (Observation 1) — small but
   removes a sharp edge.
6. **Cap config + sibling listing** (Observation 2) — make full list visible.
7. **OS-aware install** (Observation 5) — drops dead PS1 files on Linux.
8. **Session-noisy file priority** (Observation 6) — minor UX nudge.
9. **CONTEXT_STATE.md / MEMORY.md / CLAUDE.md templates** (Observation 7) — debatable; some teams may not want a templated CLAUDE.md, but a stub-with-deferral approach would be valuable.

---

---

## Post-`--update --force` verification (2026-05-13)

After applying `python -m vco_lib.project_init install-bundle --update --force`:

- **Overwrote: 33 files**, **always-overwrote: 4 helper `_lib/` files**,
  noop: 113. Zero warnings, zero errors.
- 42 → effectively 0 remaining drift. (7 agent files still differ from
  templates because the templates contain `{{ORCHESTRATOR_ROOT}}` placeholders
  that VCO substitutes at install time — SD15 now has the substituted
  versions. Not real drift.)
- `context-size-check.sh` confirmed updated: `MAX_LINES=200` → `MAX_LINES=400`.
  Future sessions won't fire the false-positive context-size warning.
- `code-graph-updater.md` confirmed identical to VCO template
  (stale "ARTup" reference gone, YAML frontmatter present).

**Bug found during verification (Gap 11)**:

`UPDATE_DEFERRED.md` still exists post-`--update --force` with the same
content as before. **VCO did not delete or update the deferral file after
applying the resolution.**

`.claude/.vco-manifest.json` mtime updated to `01:03` (current install) — so
the install ran. But `UPDATE_DEFERRED.md` mtime is still `20:17` (the
original install time). Either:

- The bundle-install code path doesn't know about the deferral file (the
  deferral writer is separate from the install runner), or
- The `DeferralReport.write` self-cleaning contract (per
  `deferral_report.py:11`: "returns False and **deletes** the file when
  entries are empty") wasn't triggered because the install path doesn't
  re-emit the deferral with empty entries — it just doesn't emit anything.

A future Claude session opening this project will read the stale
`UPDATE_DEFERRED.md` and believe there are still pending actions, even
though they've been applied. This loops back to **Gap 10**: the project
has no canonical place that says "deferrals resolved", so stale deferral
files become a source of confusion.

**Suggested fix (in priority order)**:

1. **Primary**: After a successful `install-bundle --update --force`, re-emit
   the deferral report with current state (which should result in zero
   entries → the file gets deleted). Single call to `DeferralReport(folder).write(folder)`
   after the install loop completes is probably sufficient.

2. **Defense in depth — ship a cleanup script**: VCO should also bundle a
   `.claude/scripts/cleanup_deferral_leftovers.py` (or similar) that the
   `UPDATE_DEFERRED.md` file itself REFERENCES in a "last step" section:

   ```markdown
   ## Cleanup (run AFTER resolving the deferral above)

   The apply command above may not delete this file or related state.
   Run this to clean up all VCO-deferral leftovers:

   ```bash
   python .claude/scripts/cleanup_deferral_leftovers.py
   ```

   This script:
   - Deletes `.claude/context/UPDATE_DEFERRED.md` (this file)
   - Strips `<!-- vco-deferral-reminder-* -->` blocks from CLAUDE.md
     and CONTEXT_STATE.md (see Gap 10)
   - Deletes `.vco-shipped` sidecar files for files that no longer
     differ from shipped versions (see Observation 4)
   - Reports anything else suspicious (orphan markers, lingering
     `.vco-rejected` files, etc.)
   ```

   **Why a script instead of just fixing the bug**: Two reasons.

   a. The script is the **safety net** for the bug scenarios — Gap 7
      (manifest doesn't track preserved files), Gap 9 (no longitudinal
      history), and the primary fix above all assume well-behaved VCO
      install runs. The script handles the case where SOMETHING in
      VCO leaves state behind, regardless of cause.

   b. The script becomes the **single canonical location** for "tidy up
      VCO state". As future deferral types get added (Gap 10's
      schema-migration, Weaviate-unreachable, etc.), each emits its own
      reminder markers and possibly its own sidecar files. The cleanup
      script grows to know about each one. The deferral .md only needs
      to point at this single script — it doesn't have to enumerate
      every cleanup action inline.

   c. It makes the project **self-sufficient** for deferral resolution
      (consistent with Observation 4). The user (or Claude) never has
      to leave the project tree to clean up after a VCO action.

   The script should:
   - Live at `.claude/scripts/cleanup_deferral_leftovers.py` (consistent
     with other VCO-shipped scripts).
   - Be idempotent (re-running is a no-op).
   - Print what it did + what it found (so Claude sessions running it
     can report back to the user).
   - Exit 0 even if there was nothing to clean (idempotent).
   - Optional `--dry-run` flag.

   **Code location**: Add to `templates/scripts/cleanup_deferral_leftovers.py`
   in VCO_dev. Reference from the deferral writer in
   `vco_lib/deferral_report.py` so every generated `UPDATE_DEFERRED.md`
   has the "## Cleanup" section appended after the per-condition entries.

3. **Pair with Gap 10**: When the script is implemented, the wrapped
   CLAUDE.md reminder block (Gap 10) should also reference it as the
   official cleanup mechanism. This way: deferral exists → reminder in
   CLAUDE.md → reminder points to UPDATE_DEFERRED.md → UPDATE_DEFERRED.md
   ends with a "run the cleanup script when done" section → script
   removes both the deferral file AND the reminder block.

**Code location**: `vco_lib/project_init.py` near the end of `install_bundle`
(after all file operations succeed) for the primary fix; new
`templates/scripts/cleanup_deferral_leftovers.py` for the safety net.

---

**End of handoff.** All observations are reproducible from the SD15 project state
as of 2026-05-13. Source code references point at `VCO_dev/vco_lib/project_init.py`
and `VCO_dev/vco_lib/deferral_report.py` for the relevant code paths.
