# Update Recovery — how the orchestrator self-update protects itself, and what to do when it doesn't

> **Scope note**: this document covers the UPDATE-mechanics half
> (lockfiles, sentinels, the stage1 updater, toasts, manual recovery).
> The broader post-install verification flow (containers, MCPs, Weaviate,
> hub health) is in this same folder:
> [`POST-INSTALL-HEALTH-AUDIT.md`](./POST-INSTALL-HEALTH-AUDIT.md) (6-item
> audit) / [`CLAUDE-DIAGNOSTIC-PROMPT.md`](./CLAUDE-DIAGNOSTIC-PROMPT.md)
> (paste-into-Claude diagnostic flow) /
> [`CONTAINER-RECOVERY.md`](./CONTAINER-RECOVERY.md) (Podman / Docker /
> port conflicts) / [`WINDOWS-FIRST-RUN-CHECK.md`](./WINDOWS-FIRST-RUN-CHECK.md)
> (native-Windows quirks).

This file is written for two audiences at once: a user pasting symptoms
into Claude, and the Claude session itself trying to diagnose an update
that went sideways. Every state file below names its exact path.

---

## The four update entry paths

All four share the same protected tail:

| Path | Trigger | Shared protections |
|---|---|---|
| Update | Settings → Updates → "Update orchestrator" | MCP kill-sweep, update gate, binary-wait, stage1 handoff, gate disarm before restart |
| Merge | divergence modal → "Merge" | same |
| Rebase | divergence modal → "Rebase" | same |
| Resume | purple "Continue Update" MenuBar badge | same |

---

## Generated / release-controlled files resolve to upstream automatically

A fork often diverges from upstream on files it never hand-authored:
`launcher/package.json`, `launcher/package-lock.json`,
`launcher/src-tauri/Cargo.lock`, and anything under `launcher/dist/**`.
These are npm/cargo/build artifacts plus the upstream-bumped version field —
upstream refreshes them essentially every release, so a divergent fork
conflicts on **every** update. That is an "expected conflict", not a real
breakage.

The update flow now takes upstream's copy for those files instead of surfacing
the divergence modal:

- **Committed divergence** (a fork that committed local dep bumps or rebuilt
  binaries): resolved via a synthetic take-upstream commit. Nothing is lost —
  the commit's parent still holds the fork's content, so recovery is a plain
  `git log -- <path>` + `git checkout <sha> -- <path>`.
- **Working-tree-dirty divergence** (an uncommitted `npm install` / `cargo
  update` / local rebuild): the file is restored to HEAD before the pull, and
  the pull then brings upstream's version. The discarded content is a derived
  artifact that `npm install` / `cargo build` / `install.py --update`
  re-derives.

A divergent **source** file (`Cargo.toml`, `tauri.conf.json`, `pyproject.toml`,
`vct-module.json`, `*.py`, `*.rs`) still surfaces the modal — a local edit
there is a real signal, not silently overwritten. Each reconcile writes a
`generated_files_reconciled` entry to `UPDATE_DEFERRED.md` naming exactly what
was overwritten (see the condition-id list below).

Two caveats:

- The mechanism is the launcher's Rust reconcile path (the
  `GENERATED_RELEASE_CONTROLLED_PATTERNS` class in
  `launcher/src-tauri/src/commands/git_user_editable_merge.rs`), NOT a
  `.gitattributes` merge driver: a take-theirs driver is not built into git,
  the launcher's reconcile paths are driver-blind, and drivers are
  forward-only.
- The updater that applies a release is the *installed* launcher, so the first
  update that ships this feature is still run by the prior binary and can hit
  the modal one last time; the release after that auto-resolves.

---

## State files and what each means

### `<vct_root>/.update-in-progress.json` (the "update gate", V52-AI)

- `<vct_root>` is `~/.vct` unless `VCT_STATE_DIR` overrides it.
- Written by the launcher at the START of every update path; deleted
  (deterministically, before the restart hop) at the end.
- **While fresh** (its embedded `expected_completion_by` deadline, 15 min
  from the last phase advance): every orchestrator MCP spawn exits with
  **code 75** instead of starting; the `session-start-ensure-hub` hook
  and the launcher's boot-time hub respawn also skip.
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
  so a later boot still finds the trail.
- A lock with NO `update.result.json` sibling that is >10 min old means
  the updater crashed; the next launcher boot reports "update may have
  failed" and removes it.

### `<vct_root>/update.result.json` (authoritative swap outcome)

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
    the hub is kept stopped through the handoff, so the
    usual culprits are antivirus/indexer).
  - `parent <pid> did NOT exit within 30s — aborting swap` (exit 4;
    the lock is removed, nothing was changed on disk).
  - `outcome recorded: success=… → …update.result.json`
  - `relaunch spawned: …` / `relaunch FAILED …`

### `<install_root>/.claude/state/orchestrator-update-resume-needed.json` (resume sentinel)

- Written when a merge/rebase halts at a conflict. Drives the purple
  **"Continue Update"** MenuBar badge.
- Cleared by: clicking Continue Update (resume path), aborting the
  merge, a fresh "Update orchestrator" run, and
  by any successful terminal `python install.py --update`.
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
    fallback (overwrite, rename, stage1). Fix: fully quit the launcher
    (tray → Quit) **and** `vct-hub --stop`, then run
    `python install.py --update` again.
    *(Corrected in v0.2.91: before v0.2.91 that command could NOT repair a
    dirty dist binary — install.py had no `git checkout`/restore of
    `launcher/dist/**` at all, and its binary swap only ever fired when a
    freshly cargo-built `target/release/vct-launcher-temp` existed, which a
    binary-download user never has. v0.2.91 adds the restore leg
    (`vco_lib/dist_binary_repair.py`), so the instruction is now true. On
    an older build, use the manual recipe below instead.)*
  - `launcher_binary_stale` (v0.2.91+) — the running launcher is not the
    binary git says is on disk. The entry names all three values (running
    version, dist sidecar version, whether the dist tree diverges from HEAD)
    and states whether a swap was staged. Two shapes:
    - **the dist sidecar declares a NEWER version than the running process** —
      a swap is staged and armed. Fix: quit the launcher normally; the staged
      binary is put in place as the process exits and your next launch runs it.
      Nothing restarts itself.
    - **the versions MATCH and only the dist tree is dirty** — the entry is
      surfaced but **nothing is staged and nothing is armed**, because an
      equal-version dirty dist slot is also what a local `cargo build` looks
      like and the launcher will not overwrite your own build from HEAD
      unasked. Fix: `python install.py --update` when you do want HEAD's
      binaries back, or leave it alone if the divergence is your build.
  - `launcher_binary_handoff_skipped_dirty` (v0.2.91+) — HEAD's bytes were
    staged as `<target>.new` but the stage1 handoff did not fire (no
    `vct-updater.exe`, spawn failure). Fix: the manual recipe below.
  - `launcher_binary_clobber_averted` (v0.2.91+) — informational record: an
    update abort declined to restore an old binary over freshly-pulled bytes.
    No action needed.
  - `update_resume_required` — see resume sentinel above.
  - `generated_files_reconciled` — informational audit record: an update
    reconciled diverged generated / release-controlled files
    (`launcher/package.json`, `launcher/package-lock.json`,
    `launcher/src-tauri/Cargo.lock`, or files under `launcher/dist/**`) to
    upstream automatically (see the take-upstream section above). It names each
    overwritten path, the action taken, and how to recover the committed
    case. No action needed; it self-clears on the next `install.py --update`.

---

## Decision table for a confused state

| You observe | Most likely state | Action |
|---|---|---|
| MCPs exit 75, no update running | stale `.update-in-progress.json` | wait ≤15 min OR delete the file (conditions above) |
| "Update may have failed (swap_failures=…)" toast | a binary swap hit a lock | read `~/.vct/update.log`; close launcher; `python install.py --update` |
| "Update may have failed (updater_crashed…)" toast | vct-updater died mid-swap | same as above; the lock has been cleaned for you |
| Purple Continue Update badge after finishing manually | resume sentinel survives | v0.2.54+: run `python install.py --update`; older: delete the sentinel |
| Launcher restarts into the OLD version repeatedly | dist binary still stale (lock) on Windows; on Intel Macs pre-v0.2.54, the arm64-hardcoded dist path | update to ≥v0.2.54; Windows: quit fully, let vct-updater swap |
| **Update says "Already up to date", source IS current, but the launcher version never changes** | **stale dist binary (frozen exe) — see the stale-exe recipe below** | **v0.2.91+ heals it at boot/update-check; on older builds use the manual recipe** |
| Hub still on old version after update | pre-v0.2.54 hub-restart-before-staging ordering | `vct-hub --stop` then relaunch the launcher |

---

## Stale-exe recovery (the launcher is frozen on an old binary)

**How to recognise it**: updates keep succeeding — often reporting "Already up
to date" — `git log` shows your clone tracking upstream, yet the launcher's
version never moves and bugs fixed several releases ago keep reproducing. The
give-away is `git status --porcelain -- launcher/dist/<arch>/` reporting ` M`
rows: git thinks the bytes it wrote are not the bytes on disk.

**How it happens**: a Windows binary swap fails (antivirus/indexer holding a
handle, a pull that skipped the locked `.exe`, or — before v0.2.91 — an update
abort restoring the old exe over freshly-pulled bytes). The dist binary is then
diverged from HEAD, and before v0.2.91 nothing ever looked at it again.

**v0.2.91 heals this on its own**: the launcher probes the binary at boot and at
update-check time and writes a `launcher_binary_stale` entry naming the running
version, the on-disk version, whether the dist tree diverges from HEAD, and the
single manual action. When the dist sidecar declares a **newer** version than
the running process — the frozen-exe shape described above — it also stages
HEAD's binary as `<target>.new` and arms a swap for your next quit. When the
versions match and only the tree is dirty, it surfaces the state but stages
nothing (that shape is indistinguishable from a local `cargo build`, and your
build is yours). It never restarts or quits itself, and it stands down entirely
while an update is running.

**Manual recipe** (any version; needs no working launcher):

```bash
# 0. FIRST, copy these aside — they say what actually failed:
#    ~/.vct/update.log         (%USERPROFILE%\.vct\update.log on Windows)
#    <install>/state/logs/install.jsonl

# 1. Fully quit the launcher (tray -> Quit), then stop the hub.
vct-hub --stop
#    Confirm nothing is running: no vct-launcher / vct-hub / vct-updater.

# 2. From the orchestrator install root:
git status --porcelain -- launcher/dist/    # expect ` M` rows
git pull                                    # only if the tree is behind
git checkout -- launcher/dist/              # HEAD's binaries land on disk

# 3. Remove leftovers from the failed swap (safe only while nothing runs):
#    launcher/dist/<arch>/*.new
#    launcher/dist/<arch>/vct-*.old-*
#    ~/.vct/update.lock.json  and  ~/.vct/update.result.json

# 4. Reconcile hooks / MCP registrations against the current source, then
#    relaunch through your usual entrypoint.
python install.py --update
```

Step 2's `git checkout` is the whole fix whenever nothing holds the file open —
which is why step 1 matters.

On v0.2.91+ `python install.py --update` does **step 2's restore for you**: it
runs `git checkout HEAD --` on every dist file git reports as *tracked*-modified
and, when a file is locked and cannot be rewritten, writes HEAD's bytes to
`<target>.new` and hands off to the stage1 updater instead.

**Step 3 is still yours.** The repair leg never touches untracked files —
deliberately, because `*.new`, `vct-*.old-*` and the lock/result files under
`~/.vct/` are exactly the untracked set, and a repair pass that deleted
untracked paths would be free to delete a staged binary somebody is waiting on.
Nothing in the product removes them; delete them by hand while nothing is
running.

---

## Ordering guarantees (for maintainers)

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

### v0.2.91: one home for the delivery chain, and repair at rest

Step 3's staging + handoff pair now lives in
`launcher/src-tauri/src/services/binary_freshness.rs` and is called from BOTH
update surfaces (`installer::finalize_update_and_restart` and
`self_update::finish_apply_after_pull`), so they cannot drift. The same module
owns the Windows pre-pull rename, its **non-clobbering** revert, and the
at-rest reconcile.

Two invariants worth keeping:

- **An abort never blind-restores.** `revert_pre_pull_rename` hash-compares the
  `<name>.old-<pid>` backup against the canonical path and keeps the canonical
  bytes when they differ (the pull already landed a newer binary there). Both
  legs are unit-tested; do not "simplify" it back to an unconditional rename.
- **At-rest reconcile never restarts or quits.** Boot and update-check may
  STAGE a binary and arm a swap, but the swap only runs from the
  `RunEvent::Exit` hook, and its updater lock carries `relaunch: None`. It also
  stands down when an update handoff already owns `~/.vct/update.lock.json`, so
  a real update's relaunch is never dropped.

The terminal-side equivalent is `vco_lib/dist_binary_repair.py`
(restore-then-stage over `git checkout -- <path>` / `git show HEAD:<path>`),
called from `install.py::_repair_dist_from_head_leg`. It is the escape hatch
for the bootstrap paradox: every launcher-side fix ships inside the launcher
binary, so the terminal path has to work when that binary is the broken part.
