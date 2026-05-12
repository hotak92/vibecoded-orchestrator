# kg-sync stdio-deadlock fix — 2026-05-12 (v0.2.x follow-up)

## Problem

`launcher/src-tauri/src/commands/kg_sync.rs::run_subprocess` drained
stdout line-by-line for live progress events, then drained stderr only
**after** stdout had closed. The Python subprocess (`sync_knowledge_graph.py`)
pipes both stdout and stderr. Linux pipe buffers are ~64 KiB by default.

Sequence of the wedge (confirmed empirically against SD15 on 2026-05-12):

1. The script emits enough stderr (weaviate-client retry chatter,
   urllib3 warnings, traceback prefixes) to fill the 64 KiB pipe buffer.
2. Python's next stderr write blocks inside `anon_pipe_write` waiting
   for the launcher to drain.
3. Because Python is blocked, it stops emitting stdout.
4. The launcher's stdout-reader `.next_line().await` quiesces (no
   error, no close — just nothing).
5. The launcher's stderr-reader doesn't start (sequenced after
   stdout-EOF).
6. **Deadlock.** Observed: Python (PID 1321628) stuck in
   `S anon_pipe_write` with 20 futex_do_wait threads; launcher
   (PID 1321234) held both pipe fds with the stderr fd showing a full
   pipe; sync wedged indefinitely at "embedding 14/68".

Killing Python externally was undetected by the launcher — both
readers were stuck.

The misleading comment at the old line 559-561 *"no concurrent stderr;
we drain stderr after the process exits to keep parsing single-threaded
and deterministic"* baked the broken assumption directly into the code.

## Approach

**Concurrent drain via `tokio::sync::mpsc` + tagged channel** (precedent:
`commands::runtime_install.rs` already uses two `tauri::async_runtime::spawn`
readers for the same shape of problem; `runtime_install` is a separate
event stream so it doesn't need tagging — we do).

- Two `tokio::spawn` reader tasks (stdout + stderr).
- Both feed a single bounded `mpsc::channel<PipeLine>(1024)`; `PipeLine`
  is `enum { Stdout(String), Stderr(String) }`.
- Main loop awaits `rx.recv()` and dispatches parsing only on `Stdout`
  variants — preserving the existing single-threaded, deterministic
  parse semantics. Stderr lines accumulate into the same `combined`
  buffer the post-exit drain used to populate, so `tail_log` and the
  crash-snippet logic see them.
- Drop the original sender after spawning so the channel closes once
  both reader tasks finish.
- After the loop: `JoinHandle::await` both reader handles, then
  `child.wait().await` as before.

**Soft stall watchdog** layered on top via `tokio::time::timeout`:

- `KG_SYNC_STALL_TIMEOUT_SECS` env var (default 300 s; 0 disables).
- Each `recv()` is wrapped in `tokio::time::timeout(watchdog, ...)`.
- On timeout: set `stalled = true`, call `child.start_kill()` (the
  non-blocking cross-platform variant), break the loop.
- On the failure-return path, reconcile optimistic counts (stage-aware
  Bug-2 logic), produce a tailored error message:
  `"kg-sync stalled (no output for {N}s); subprocess killed. Set
  KG_SYNC_STALL_TIMEOUT_SECS to override (0 disables the watchdog)."`

**Sibling fix in `kg_summary.rs::invoke_summariser_once`**: same
structural defect (sequential `read_to_string` on stdout then stderr).
Per-file output is bounded so an actual wedge is unlikely, but the
fix is trivial: wrap both reads in `tokio::join!` so they drain
concurrently. Applied for consistency / defense-in-depth.

`codegraph.rs::run_build_task` was audited and is safe — it uses
`cmd.output().await`, which Tokio internally drains both pipes
concurrently into `Vec<u8>`.

## Files changed

1. `launcher/src-tauri/src/commands/kg_sync.rs` — concurrent
   stdout/stderr drain via `mpsc::channel` + tagged `PipeLine` enum;
   stall watchdog with `KG_SYNC_STALL_TIMEOUT_SECS` env var;
   `resolve_stall_timeout()` helper; `DEFAULT_STALL_TIMEOUT_SECS = 300`;
   stall-path explicit early-return with reconciled counts; six new
   tests (4 timeout-resolution, 2 subprocess-level deadlock/watchdog
   regression tests, unix-gated).
2. `launcher/src-tauri/src/commands/kg_summary.rs` — replaced
   sequential `read_to_string` pattern with `tokio::join!` of both
   reads. Comment block explains the structural-equivalence reasoning.

No other files touched. No schema migrations. No FE changes.

## Decisions

- **Channel pattern over `tokio_stream::StreamExt::merge`.** Both
  exist in tokio's ecosystem. The repo doesn't already use
  `tokio_stream::merge` and adding a new dep / idiom for this fix
  felt heavier than warranted. The channel pattern is also strictly
  more flexible if we ever want to backpressure-throttle progress
  emission. The bounded-1024 channel covers worst-case bursts
  (~120 KiB in flight worst case) without enabling new failure modes.
- **`tokio::spawn` rather than `tauri::async_runtime::spawn`.** Both
  work in this codebase; the surrounding code in `kg_sync.rs` already
  uses `tokio::spawn` (line 164 — the top-level `spawn_initial_sync`),
  so the fix matches its file-local convention. `runtime_install.rs`
  uses the other variant; that's a different file's conventions.
- **Stall watchdog default 300 s, not 60 or 120.** Slowest path we've
  observed: an Ollama node embedding ~8 KiB on a CPU-only host under
  load takes ~30-60 s; a long PDF appendix that triggers a half-dozen
  embedding batches can plausibly produce 3-5 min of silence between
  stdout markers. 300 s leaves substantial headroom. Configurable via
  env if anyone hits a slower path.
- **Stall watchdog uses `child.start_kill()` not `child.kill().await`.**
  `start_kill` is non-blocking and cross-platform (SIGKILL on Unix,
  `TerminateProcess` on Windows). We don't await the kill because we
  fall through to `child.wait().await` immediately after — Tokio
  reaps the now-zombie process there.
- **Reconcile optimistic counts on stall.** A stalled subprocess by
  definition emitted nothing for `timeout` seconds, so we definitively
  did NOT see the `📊 KG:` / `📊 Docs:` summaries. Mirroring the
  post-exit reconcile keeps banner counts honest (no inflated
  `succeeded` from optimistic `🔄 Syncing node:` increments).
- **Apply the kg_summary.rs fix too.** The user's audit said per-file
  output is bounded so it "won't wedge", but the structural defect
  is identical and `tokio::join!` is a one-line change. Defense in
  depth — also catches the case where the summariser's claude-cli
  backend produces a long traceback to stderr.
- **Cross-OS verification on the Tokio drain layer alone.** The fix is
  pure Rust async — no platform-specific syscalls. Tokio normalizes
  `AsyncBufReadExt::lines()`, `Child::start_kill`, `Child::wait`,
  and `mpsc::channel` across platforms. The unix-gated tests
  (`#[cfg(unix)]`) exercise the drain logic with real OS pipes; on
  Windows we trust the same Tokio APIs as runtime_install.rs already
  does.

Nothing escalated to the user — the established patterns covered every
question that came up.

## Verification

`cargo test --lib`:
- **All 585 tests pass, 0 failed, 1 ignored** (pre-existing).
- `commands::kg_sync::tests` group: **25 passing** (up from 19),
  including 4 new timeout-resolution tests and 2 unix-gated
  subprocess regression tests.
- New `concurrent_drain_does_not_deadlock_on_large_stderr` reproduces
  the exact wedge condition: a sh script writes 128 KiB to stderr
  (2x the 64 KiB Linux default pipe buffer) BEFORE any stdout, then
  writes 3 stdout lines. Pre-fix sequencing would deadlock; the test
  has a 15 s hard timeout via `tokio::time::timeout` and asserts all
  3 stdout lines + all 2048 stderr lines arrive.
- New `stall_watchdog_kills_silent_subprocess` spawns `sleep 30` with
  a 1 s watchdog and asserts (a) the watchdog trips, (b) the
  subprocess is killed within 5 s wall-clock, (c) the subprocess
  reports failure on exit.

`npm run check`:
- **483 files, 0 errors, 38 warnings** — all pre-existing
  accessibility / unused-CSS warnings in `.svelte` files; no Rust
  surface affected.

## Manual test plan

1. **Reproduce the SD15 wedge fix** (the original incident): trigger
   a kg-sync against the SD15 worktree (or any project with > 50
   nodes including some that produce weaviate-client warnings).
   Expected: completes; if it fails, the failure row is populated
   with a real error (HTTP 422 etc.), not a permahung "running".
2. **Stall recovery**: temporarily set `KG_SYNC_STALL_TIMEOUT_SECS=10`,
   point kg-sync at a project, then `kill -STOP <python_pid>` mid-run.
   Expected: after 10 s the launcher transitions the row to `failed`
   with error_message matching the stall pattern, and the Python
   subprocess is killed (or zombies, on `SIGSTOP` — that's a kernel
   quirk of `SIGSTOP`, not the fix).
3. **Watchdog disable**: set `KG_SYNC_STALL_TIMEOUT_SECS=0`. Run
   normal sync. Expected: completes as before; no watchdog behavior
   triggers.
4. **Garbage env**: set `KG_SYNC_STALL_TIMEOUT_SECS=banana`. Run
   sync. Expected: warning in stderr about falling back to default;
   sync runs with 300 s watchdog. (This path is unit-tested too.)
5. **Windows smoke** (when next on a Windows host): run a kg-sync
   against a project of 20+ nodes. Expected: completes; concurrent-
   drain doesn't introduce Windows-specific regressions. We don't
   currently expect Windows-specific failures because the fix is
   Tokio-native; this is a smoke check, not a primary verification.

## Known limitations

- **Windows orphan handling on stall**: `child.start_kill()` calls
  `TerminateProcess` on the immediate child only. If
  `sync_knowledge_graph.py` ever spawned grandchildren (it currently
  doesn't — it does HTTP via weaviate-client and Ollama only), they'd
  be orphaned on stall-kill. If a future version of the script adds
  shell-outs we'd need to switch to a Windows job-object or
  `taskkill /T` invocation. Not a concern today; flagged for review
  next time the script gains subprocess children.
- **Stall watchdog is per-pipe-silence, not per-overall-progress.**
  A subprocess that emits one stderr line every 290 s would never
  trigger the watchdog even if it's making zero real progress. This
  is intentional: distinguishing "real" progress from "log spam" is
  out of scope here, and would require parsing semantics that belong
  in the script, not the launcher. The complement to this fix is
  per-script timeouts inside `sync_knowledge_graph.py` itself, which
  is a separate concern.
- **The 64 KiB pipe-buffer figure is Linux-default.** macOS uses
  16 KiB initially (can grow to 64 KiB); Windows pipes have their
  own buffering semantics. The fix doesn't care about the exact
  threshold — concurrent drain removes the back-pressure deadlock at
  ANY buffer size. The unit test uses 128 KiB to be comfortably
  over even the Linux maximum.

## v0.2.x version policy

Per the user directive, no version bump for this cycle. Ship as a
focused fix branch to main.
