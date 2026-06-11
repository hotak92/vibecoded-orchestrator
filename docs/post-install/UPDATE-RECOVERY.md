# Update Recovery — how the orchestrator self-update protects itself, and what to do when it doesn't

> **Scope note (v0.2.54)**: this document covers the UPDATE-mechanics half
> (lockfiles, sentinels, the stage1 updater, toasts, manual recovery).
> The broader post-install verification flow (containers, MCPs, Weaviate,
> hub health) is Track F's `POST-INSTALL-HEALTH-AUDIT.md` /
> `CLAUDE-DIAGNOSTIC-PROMPT.md` in this same folder — if those files
> don't exist yet in your checkout, they're planned; this file stands on
> its own for update issues.

This file is written for two audiences at once: a user pasting symptoms
into Claude, and the Claude session itself trying to diagnose an update
that went sideways. Every state file below names its exact path.

---

## The four update entry paths

All four share the same protected tail since v0.2.54 (P0-7 hoist):

| Path | Trigger | Shared protections |
|---|---|---|
| Update | Settings → Updates → "Update orchestrator" | MCP kill-sweep, update gate, binary-wait, stage1 handoff, gate disarm before restart |
| Merge | divergence modal → "Merge" | same (added v0.2.54 — previously had none) |
| Rebase | divergence modal → "Rebase" | same (added v0.2.54 — previously had none) |
| Resume | purple "Continue Update" MenuBar badge | same (gate disarm added v0.2.54) |

---

## State files and what each means

### `<vct_root>/.update-in-progress.json` (the "update gate", V52-AI)

- `<vct_root>` is `~/.vct` unless `VCT_STATE_DIR` overrides it.
- Written by the launcher at the START of every update path; deleted
  (deterministically, before the restart hop) at the end.
- **While fresh** (its embedded `expected_completion_by` deadline, 15 min
  from the last phase advance): every orchestrator MCP spawn exits with
  **code 75** instead of starting; the `session-start-ensure-hub` hook
  and the launcher's boot-time hub respawn also skip (v0.2.54 C-7).
- **Symptom: "all my MCPs exit 75"** → this file is present and fresh.
  - During a real update: expected, resolves itself within minutes.
  - With NO update running: the previous update crashed inside its
    15-min window. Safe manual clear conditions: no `vct-launcher`
    update flow is on screen AND `ps`/Task Manager shows no
    `vct-updater` process. Then: `rm ~/.vct/.update-in-progress.json`.
    (If you wait instead, the launcher's next boot self-heals once the
    deadline lapses.)

### `<vct_root>/update.lock.json` (stage1 handoff contract, V52-AH, Windows)

- Written by the launcher (or terminal `install.py --update`) when a
  binary on disk is still Windows-locked and `vct-updater.exe` must
  perform the swap after the launcher exits.
- Consumed by the updater on full success. **Kept on swap failure**
  (v0.2.54 C-4) so a later boot still finds the trail.
- A lock with NO `update.result.json` sibling that is >10 min old means
  the updater crashed; the next launcher boot reports "update may have
  failed" and removes it.

### `<vct_root>/update.result.json` (authoritative swap outcome, v0.2.54 C-4)

- Written by `vct-updater` AFTER the swaps and BEFORE relaunching the
  launcher. Shape:
  `{ "success": bool, "swaps_attempted": N, "swap_failures": N, "completed_at": "unix:...", "detail": "..." }`
- The relaunched launcher's boot probe reads this file (never guesses
  from timestamps), caches the report in app state, and the frontend
  shows either "Launcher updated to vX" or "Update may have failed
  (swap_failures=N…) — see update.log".
- One-shot: consumed at boot. If you see it on disk while no launcher is
  running, the relaunch failed — just start the launcher manually; the
  toast will tell you what happened.

### `<vct_root>/update.log` (updater forensic trail)

- Plain text, overwritten each handoff. Lines to look for:
  - `swap OK: <path>` / `swap FAILED <path>: MoveFileExW failed: GetLastError=32`
    (32 = ERROR_SHARING_VIOLATION → something still held the binary;
    since v0.2.54 the hub is kept stopped through the handoff, so the
    usual culprits are antivirus/indexer).
  - `parent <pid> did NOT exit within 30s — aborting swap` (exit 4;
    the lock is removed, nothing was changed on disk).
  - `outcome recorded: success=… → …update.result.json`
  - `relaunch spawned: …` / `relaunch FAILED …`

### `<install_root>/.claude/state/orchestrator-update-resume-needed.json` (resume sentinel, v0.2.51 Bug A)

- Written when a merge/rebase halts at a conflict. Drives the purple
  **"Continue Update"** MenuBar badge.
- Cleared by: clicking Continue Update (resume path), aborting the
  merge, a fresh "Update orchestrator" run, and — since v0.2.54 (C-6) —
  by any successful terminal `python install.py --update` (the
  deferral's "Option B" promise is now true).
- **Symptom: badge persists after you already finished the update** →
  pre-v0.2.54 behaviour; either update the orchestrator or delete the
  sentinel file manually (safe once `git status` in the install root
  shows no in-progress merge/rebase).

### `<install_root>/.claude/context/UPDATE_DEFERRED.md`

- Conditions an `install.py --update` could not auto-resolve. Each entry
  carries `condition_id`, what was detected, why it was deferred, and an
  explicit `command_to_apply`. Common update-related ids:
  - `launcher_restart_required` — new binary on disk, old binary still
    executing. Fix: quit (tray → Quit) + relaunch.
  - `launcher_binary_swap_failed_locked` — Windows lock beat every
    fallback (overwrite, rename, stage1). Fix: close the launcher, run
    `python install.py --update` again.
  - `update_resume_required` — see resume sentinel above.

---

## Decision table for a confused state

| You observe | Most likely state | Action |
|---|---|---|
| MCPs exit 75, no update running | stale `.update-in-progress.json` | wait ≤15 min OR delete the file (conditions above) |
| "Update may have failed (swap_failures=…)" toast | a binary swap hit a lock | read `~/.vct/update.log`; close launcher; `python install.py --update` |
| "Update may have failed (updater_crashed…)" toast | vct-updater died mid-swap | same as above; the lock has been cleaned for you |
| Purple Continue Update badge after finishing manually | resume sentinel survives | v0.2.54+: run `python install.py --update`; older: delete the sentinel |
| Launcher restarts into the OLD version repeatedly | dist binary still stale (lock) on Windows; on Intel Macs pre-v0.2.54, the arm64-hardcoded dist path | update to ≥v0.2.54; Windows: quit fully, let vct-updater swap |
| Hub still on old version after update | pre-v0.2.54 hub-restart-before-staging ordering | `vct-hub --stop` then relaunch the launcher |

---

## Ordering guarantees (v0.2.54, for maintainers)

The shared finalize tail (`installer.rs::finalize_update_and_restart`)
enforces, in order:

1. `WaitForBinaryRefresh` (don't restart into a stale binary, V45-B);
2. update-gate disarm (BEFORE any exit hop — `app.exit(0)` can kill the
   process before RAII `Drop` runs on Windows);
3. stage locked binaries (`<target>.new`) + handoff decision;
4. **handoff active** → exit with the hub still STOPPED (so
   `vct-hub.exe` is swappable; the relaunched launcher starts the new
   hub) — **no handoff** → start the hub, then `restart_launcher`.

`vct-updater` enforces: swaps → write `update.result.json` → delete
lock iff full success → relaunch → write `update.log`. The result file
preceding the relaunch is what makes the post-update toast reliable.
