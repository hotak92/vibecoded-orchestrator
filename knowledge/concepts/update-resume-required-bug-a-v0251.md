---
title: Orchestrator Update Resume — Bug A (v0.2.51)
type: concept
tags: [installer, launcher, update-flow, recovery, bug-fix, v0251]
created: 2026-06-09T00:00:00Z
updated: 2026-06-09T00:00:00Z
valid_from: 2026-06-09T00:00:00Z
valid_until: null
status: active
---

# Orchestrator Update Resume — Bug A (v0.2.51)

## Problem

Pre-v0.2.51, the launcher's "Update orchestrator" flow could halt mid-stream
when `git pull` / `merge` / `rebase` produced a conflict and never re-enter
the post-merge tail. The conflict modal had two paths:

- **Abort & restore** — calls `abort_orchestrator_merge_or_rebase` → works.
- **Resolve manually** — JUST closed the modal. No Tauri command. No resume.

After the user resolved the conflict in their editor (or CLI `git add` +
`git commit`), the launcher had no record of the in-flight update. Result:
merged source on disk, OLD binary running, stale `state/install-manifest.json::version`.
Clicking the "Install Update" badge re-triggered `update_orchestrator`,
but the git pull was now a no-op (already merged), so the function exited
early without re-running install.py either.

## Fix architecture

Five-piece change:

### 1. Resume sentinel (Rust)

Conflict-surfacing sites (`update_orchestrator`, `merge_orchestrator_with_upstream`,
`rebase_orchestrator_onto_upstream`) write a JSON sentinel at
`.claude/state/orchestrator-update-resume-needed.json` BEFORE returning the
conflict payload to the GUI. Fields: `schema` (1), `operation`
(`"merge"` / `"rebase"`), `branch`, `sha_at_conflict`, `written_at`.
Used as the "checkpoint" the resume command reads to verify HEAD has
actually advanced past the conflict.

### 2. Status detection (Rust)

`check_for_updates` reads the sentinel + probes `.git/MERGE_HEAD` /
`.git/rebase-merge/` / `.git/rebase-apply/`. The new
`UpdateStatus.merge_resolved_incomplete: bool` is true ONLY when the
sentinel is present AND no in-flight merge state exists (= user resolved
the merge but launcher never re-entered the install). Highest-priority
kind in `UpdateBadge.svelte`.

### 3. Resume Tauri command (Rust)

`resume_orchestrator_update` performs:

1. Read sentinel → bail with structured error if absent (= no resume pending).
2. Reject if `.git/MERGE_HEAD` exists (= merge still in progress).
3. Reject if HEAD SHA == `sha_at_conflict` (= user aborted via CLI; sentinel stale).
4. Reject if `git grep -E '^(<{7}|={7}|>{7}) '` returns files (= user
   committed without resolving markers).
5. Audit-log `update_orchestrator_resumed` for forensic clarity.
6. Stop hub + pre-pull-rename binaries.
7. Clear sentinel BEFORE install.py (so a crash doesn't loop us).
8. Call `run_post_pull_install_and_restart` (install.py --update + binary
   refresh + auto-restart).

Single-flight via `RESUME_IN_FLIGHT: LazyLock<tokio::sync::Mutex<()>>` so
rapid clicks don't race two install.py runs.

### 4. UpdateBadge — `merge_resolved_incomplete` kind

Highest priority (above `binary_stale`). Visual: purple
(`var(--color-purple, #7b5fff)`) with a pulse animation so the user
can't miss the unusual state. Title: "Continue Update". Calls
`updater.resumeUpdate()` → `resume_orchestrator_update`.

### 5. UPDATE_DEFERRED.md mirror

Rust writes a comprehensive deferral entry directly into
`.claude/context/UPDATE_DEFERRED.md` alongside the sentinel. Shape matches
`vco_lib/deferral_report.py::_render_entry` so the Python `DeferralReport.read()`
parses it round-trip. Contains a "For your Claude assistant" section so
terminal Claude sessions surface the state at session start (the deferral
auto-injects a reminder block into CLAUDE.md via the existing emitter
chain).

The Python emitter `_emit_update_resume_required_deferral` in `install.py`
mirrors the Rust writer; useful when install.py itself wants to re-emit
during `--apply-deferred`.

## State machine

```
                  user clicks Update Orchestrator
                              │
                              ▼
                  update_orchestrator runs git pull
                              │
        ┌─────────────────────┴─────────────────────┐
        ▼                                           ▼
   pull succeeds                          pull conflicts
        │                                           │
        ▼                                           ▼
  install.py --update                  write sentinel + deferral
        │                              return conflict modal
        ▼                                           │
   binary refresh + restart                         ▼
        │                              user resolves OR aborts
        ▼                                           │
       DONE                       ┌─────────────────┴─────────────────┐
                                  ▼                                   ▼
                            user aborts                       user resolves manually
                            │                                   (git commit / editor)
                            ▼                                           │
                  clear sentinel + deferral                              ▼
                  return to Up to date                       Modal poll detects
                                                              clean working tree
                                                                        │
                                                                        ▼
                                                              "Continue Update" button activates
                                                                        │
                                                                        ▼
                                                              resume_orchestrator_update
                                                              (or persists to badge after dismiss)
                                                                        │
                                                                        ▼
                                                              install.py + binary refresh + restart
                                                                        │
                                                                        ▼
                                                                       DONE
```

## Why the conflict modal polls instead of relying only on the badge

The modal stays open while the user resolves the conflict (a common
real-world UX: keep the modal visible as a checklist of files to fix).
Polling `check_for_updates` every 2 s lets us flip the "Continue Update"
button from disabled → enabled the moment the working tree is clean,
without the user needing to dismiss + re-find the badge.

The badge is the always-on fallback: detectable on launcher startup,
between sessions, and after the modal is dismissed. The two surfaces
are intentionally redundant.

## Idempotency of `clear_update_resume_deferral_if_solo`

ONLY removes the file when it contains a single entry with `condition_id`
`update_resume_required`. Conservative: if install.py has added other
entries, we leave the file alone and let install.py's
`DeferralReport.mark_resolved("update_resume_required")` +
`DeferralReport.write()` reconcile surgically.

## Related

- [[implements::vco_lib.deferral_report.DeferralReport]]
- [[buildsOn::OrchestratorUpdateConflictModal (v0.2.23)]]
- [[buildsOn::UpdateBadge three-state design (v0.2.16)]]
