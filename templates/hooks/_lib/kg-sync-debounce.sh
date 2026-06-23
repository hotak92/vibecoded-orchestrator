# shellcheck shell=bash
# kg-sync-debounce.sh — coalesce rapid re-edits of the same file into one
# Weaviate-write per quiet-window, instead of one write per edit.
#
# WHY THIS EXISTS (write-amplification, 2026-06-18)
# -------------------------------------------------
# post-file-edit.sh fires on EVERY Edit/Write. For knowledge/, docs/ and
# code files it spawned an IMMEDIATE background sync (kg-sync /
# upload_docs.py / code-graph-incremental) on each fire. An agent that
# edits the same file 5× in a minute therefore produced 5 Weaviate
# upserts of (mostly) the same object. Combined with Weaviate's
# flush-on-timer + LSM compaction, this is a primary driver of write
# amplification (a real install rewrote ~290 GB of disk for ~5 GB of
# logical data in 12 h). The Weaviate-side tuning landed in d43acf1f;
# THIS is the complementary app-layer fix: emit fewer, coalesced syncs.
#
# CORRECTNESS ARGUMENT (the final state ALWAYS syncs)
# ---------------------------------------------------
# Debounce here means COALESCE rapid repeats, never DROP a sync.
#   * On the first edit of a file we atomically claim a per-file lock
#     (mkdir is atomic on POSIX) under
#     $STATE_DIR/kg_sync_pending/<key>.lock and spawn ONE background
#     flusher: `sleep N; <run the real sync>`.
#   * The flusher removes the lock at the START of the actual sync. So:
#       - A re-edit DURING the sleep window finds the lock present and is
#         a no-op — the already-scheduled flusher will sync the file. The
#         flusher runs the real sync command which re-reads the file FROM
#         DISK at run time (kg-sync → sync_knowledge_graph.py reads the
#         path fresh; code-graph analyzer diffs HEAD~1..HEAD fresh), so
#         it picks up the LATEST content, not the content at schedule
#         time. -> latest state syncs exactly once.
#       - A re-edit AFTER the lock cleared (i.e. the sync already started
#         or finished) finds no lock and schedules a FRESH flusher. ->
#         the post-window edit also syncs. No edit is ever lost.
#   * A file edited once and then left alone syncs exactly once, N
#     seconds after the edit. Bounded delay, never "never".
#
# CRASH-SAFETY / NO-ORPHANS
# -------------------------
# The flusher is a short-lived backgrounded subshell (sleep N then one
# sync). It is NOT a long-running daemon, so it cannot accumulate as a
# zombie pool. Two failure modes and how they're handled:
#   1. The flusher itself crashes / is killed mid-sleep (e.g. the whole
#      terminal/process-group dies). Its lock would be left behind and a
#      future edit of the SAME file would see a stale lock and skip
#      forever. To prevent a permanently-unsynced file, every call first
#      runs `_kg_debounce_reap_stale`: any lock whose mtime is older than
#      N + GRACE seconds is treated as abandoned — we recover it and run
#      its sync NOW (backgrounded). The NEXT edit to ANY debounced file
#      thus recovers ALL abandoned pending syncs. (Session-start could
#      also reap, but reap-on-next-edit needs no extra hook wiring and
#      covers the common "agent keeps editing" case immediately.)
#   2. The session ends cleanly right after an edit, before the sleep
#      elapsed. The backgrounded flusher is reparented to init (POSIX)
#      and completes independently of the parent hook — the sync still
#      lands. (On the rare hard-kill-the-process-group case, mode 1's
#      reap recovers it on the next session's first edit.)
#
# EXACTLY-ONCE UNDER CONCURRENCY (the atomic-claim invariant, 2026-06-18)
# ----------------------------------------------------------------------
# Both the normal flusher-completion path AND the reaper recover a lock
# through the SAME atomic step: rename the lock dir aside to
# "<lock>.claimed.<pid>" via `mv`. rename(2) within one directory is
# atomic on POSIX — for a given source path EXACTLY ONE caller's `mv`
# succeeds; every other caller's `mv` fails (source already gone) and
# that caller no-ops. So for each scheduled sync there is exactly one
# winner that runs the cmd, regardless of how many processes (a live
# flusher + one or more concurrent reapers) race to recover the lock.
# This closes two races that the old "read cmd, then rm -rf lock" order
# left open:
#   * Two concurrent reapers (two parallel agents editing different files
#     fire two schedule calls → two reap passes) could both read the
#     persisted cmd of the SAME orphan before either deleted it, then
#     both eval it → 2 redundant upserts. Now both attempt the same `mv`;
#     one wins, the other's `mv` fails → 1 upsert.
#   * A LIVE flusher whose `sleep` stretched past window+GRACE (laptop
#     suspend/resume, heavy load, wall-clock jump) still holds its lock,
#     so the reaper treats it as orphaned and recovers it, while the
#     still-alive flusher also wakes to run its cmd → 2 upserts. Now the
#     flusher's own completion goes through the SAME `mv` claim: whichever
#     of {the woken flusher, the reaper} wins the rename runs the cmd;
#     the loser's `mv` fails and it no-ops → 1 upsert.
#
# RESIDUAL: claimed-by-dead-pid
#   A process can die between the successful `mv` and the `eval` (the
#   window is tiny — a couple of syscalls — but non-zero). That would
#   leave a "<lock>.claimed.<pid>" dir whose cmd is claimed-but-not-run,
#   violating coalesce-NEVER-DROP if left forever. The reaper therefore
#   ALSO sweeps stale ".claimed.<pid>" dirs: if the owning <pid> is no
#   longer alive (kill -0 fails) the claim is dead → re-claim it (rename
#   to ".claimed.<our-pid>") and run it. (A pid that IS still alive is a
#   claim in flight by a live process — leave it; it will run or, if that
#   process later dies, a future pass re-orphans it. PID reuse is a
#   theoretical false-"alive": worst case the dead claim waits until the
#   reused pid also exits, then is recovered — still never dropped, only
#   delayed, matching the feature's eventually-consistent contract.)
#
# WHY sleep+background rather than a watcher daemon: zero new processes
# at rest, zero new deps, no IPC, and it degrades to "sync slightly
# later" under every failure rather than "sync never". The cost is a
# bounded N-second sync latency and (worst case, a hard process-group
# kill) a one-session delay until the next edit reaps the orphan — both
# acceptable for an eventually-consistent search index.
#
# TUNING
# ------
#   VCO_KG_SYNC_DEBOUNCE_SECONDS  quiet-window in seconds (default 5).
#                                 Set 0 to disable debounce entirely
#                                 (every edit syncs immediately, the
#                                 pre-2026-06-18 behaviour).
#
# This helper is POSIX-portable (no GNU-only flags, no bash-4 assoc
# arrays). The PowerShell sibling _lib/kg-sync-debounce.ps1 implements
# identical semantics.

# Absolute path to THIS lib, captured at source time. A detached flusher
# re-sources this file in a fresh `setsid` shell (Item 1) so it can call the
# in-process helper functions without re-inlining their exactly-once logic.
# Resolved from $0 fallbacks when BASH_SOURCE is unavailable (POSIX sh).
if [ -n "${BASH_SOURCE:-}" ]; then
    _KG_DEBOUNCE_LIB="${BASH_SOURCE}"
else
    _KG_DEBOUNCE_LIB="$0"
fi
case "$_KG_DEBOUNCE_LIB" in
    /*) : ;;  # already absolute
    *)  _KG_DEBOUNCE_LIB="$(cd "$(dirname "$_KG_DEBOUNCE_LIB")" 2>/dev/null && pwd)/$(basename "$_KG_DEBOUNCE_LIB")" ;;
esac

# POSIX single-quote escaper: wraps $1 so it survives `eval` even if it
# contains spaces or single quotes. A literal ' becomes '\'' (close,
# escaped-quote, reopen). Use this for any user-controlled path embedded
# in the command string passed to _kg_debounce_schedule.
_kg_debounce_shquote() {
    # shellcheck disable=SC1003
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# Quiet-window, in seconds. Default 5. "0" disables debounce.
_kg_debounce_window() {
    local n="${VCO_KG_SYNC_DEBOUNCE_SECONDS:-5}"
    case "$n" in
        ''|*[!0-9]*) n=5 ;;   # non-numeric → safe default
    esac
    printf '%s' "$n"
}

# State dir for pending-sync locks. Lives under the project's
# .claude/state/ (the canonical hook state dir — same one the diagram
# throttle and gate-skipped sentinels use). $1 = PROJECT_ROOT.
_kg_debounce_dir() {
    printf '%s/.claude/state/kg_sync_pending' "$1"
}

# Reaper throttle interval, in seconds. The reaper globs ALL pending work
# dirs (O(M^2) across a long-lived session), so running it on EVERY
# schedule call is wasteful when edits come in bursts. 30s = the grace
# jitter budget (`window + 30`), so even a throttled reaper still fires
# within one grace window — an orphan is recovered with at most ~grace
# extra delay, well inside the feature's eventually-consistent contract.
# MUST MATCH _lib/kg-sync-debounce.ps1 ($KG_DEBOUNCE_REAP_THROTTLE_SECONDS).
_KG_DEBOUNCE_REAP_THROTTLE_SECONDS=30

# Max number of live ".lock" dirs before we stop SCHEDULING new sleeping
# flushers and instead run the sync IMMEDIATELY in the background. The
# common burst is a handful (3-10) of distinct (file,channel) pairs edited
# rapidly; 64 simultaneous pending locks is a pathological fan-out (a
# script touching dozens of files in one tick) far above any normal agent
# edit burst, so this ceiling never trips on the common case. Above it we
# fall through to immediate-sync — we NEVER drop a sync, we only stop
# accumulating sleeping flusher subshells.
# MUST MATCH _lib/kg-sync-debounce.ps1 ($KG_DEBOUNCE_LOCK_CEILING).
_KG_DEBOUNCE_LOCK_CEILING=64

# Hash a file path → a slash-free lock key. Reuses the portable
# md5-via-Python pattern already used by the diagram throttle in
# post-file-edit.sh. $1 = path, $2 = python interpreter ($PY).
_kg_debounce_key() {
    printf '%s' "$1" \
        | "$2" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" \
        2>/dev/null || printf '%s' "_"
}

# mtime (epoch seconds) of a path, portable across GNU/BSD coreutils.
# Falls back to $2 (a "now" fallback supplied by the caller) when neither
# stat flavour works, so an unreadable stat is treated as "fresh" (never
# reaped) rather than crashing the reaper.
#   $1 = path, $2 = now-fallback epoch
_kg_debounce_mtime() {
    local m
    m=$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || printf '%s' "$2")
    case "$m" in ''|*[!0-9]*) m="$2" ;; esac
    printf '%s' "$m"
}

# Real PID of the current process, portably. In a backgrounded subshell
# `$$` is the PARENT shell's pid, so it is useless for a liveness check on
# the actual eval-runner. bash exposes the subshell's true pid as BASHPID;
# the POSIX fallback reads it via a child sh whose $PPID == us.
_kg_debounce_realpid() {
    if [ -n "${BASHPID:-}" ]; then
        printf '%s' "$BASHPID"
    else
        sh -c 'echo "$PPID"' 2>/dev/null || printf '%s' "$$"
    fi
}

# Recover ONE atomically-won work dir: re-stamp it with the eval-runner's
# REAL pid (for the dead-pid sweep), run the recorded sync, then remove it.
# The dir passed in is ALREADY owned by us (we won its rename), so no
# further locking is needed. Everything runs in ONE backgrounded subshell
# so reaping never blocks the live edit.
#
# The re-stamp is what makes the dead-pid sweep correct: the winning
# rename in the caller used the CLAIMER's $$ (which, for a flusher, is the
# short-lived parent hook), but the process that actually runs the eval is
# THIS backgrounded subshell. We rename the dir to ".claimed.<realpid>" so
# that `kill -0 <realpid>` in Sweep B truthfully reports whether the eval
# is in flight. If a process dies after this rename but before eval, the
# ".claimed.<realpid>" residual is left for Sweep B to recover.
#   $1 = won work dir (e.g. "<lock>.reaping.<token>")
#   $2 = state dir (where the ".claimed.<pid>" lands)
#   $3 = lock base name (e.g. "kg_<md5>.lock") — used to rebuild the name
_kg_debounce_run_claimed() {
    local won="$1" dir="$2" base="$3"
    (
        _rp=$(_kg_debounce_realpid)
        _claimed="$dir/$base.claimed.$_rp"
        # Re-stamp to our real pid. If the rename fails (extremely unlikely
        # — we already own $won), fall back to running from $won directly
        # so the sync is never dropped.
        if mv "$won" "$_claimed" 2>/dev/null; then
            : # owned under a liveness-accurate name
        else
            _claimed="$won"
        fi
        # CRITICAL: refresh the dir's mtime to NOW. A directory rename
        # PRESERVES mtime, so if this claim descends from a backdated
        # orphan, the ".claimed"/".reaping" successor would still look
        # `grace`-stale and a concurrent reaper's Sweep B/C could re-trip
        # on it and double-run before our eval finishes. Touching restarts
        # the grace clock at each claim transition, so an in-flight claim
        # is never treated as stranded while it is actively running.
        touch "$_claimed" 2>/dev/null || true
        _line=""
        [ -f "$_claimed/cmd" ] && _line=$(head -1 "$_claimed/cmd" 2>/dev/null || echo "")
        _wd=$(printf '%s' "$_line" | cut -f1)
        _cmd=$(printf '%s' "$_line" | cut -f2-)
        [ -n "$_wd" ] && cd "$_wd" 2>/dev/null || true
        [ -n "$_cmd" ] && eval "$_cmd" >/dev/null 2>&1 || true
        # Drop only after the eval finishes so a crash before/during eval
        # leaves a recoverable ".claimed.<realpid>" behind (Sweep B
        # recovers it) rather than dropping the sync.
        rm -rf "$_claimed" 2>/dev/null || true
    ) &
}

# Generate a per-attempt unique claim token: "<pid>.<nonce>". Used only to
# WIN the atomic rename without two concurrent claimers colliding on the
# same temp name; liveness is tracked separately via the re-stamp above.
_kg_debounce_claimtok() {
    printf '%s.%s' "$$" "${RANDOM:-$(date +%N 2>/dev/null || echo 0)}"
}

# Run a shell snippet DETACHED from the hook's process group, so it survives
# even if the hook's group is signalled during the work (the PostToolUse
# hook exits within milliseconds; a bare `( ... ) &` in the same group can
# be killed mid-`sleep`, leaving the file un-synced). The PowerShell sibling
# already detaches via Start-Process; this brings bash to parity.
#
# Strategy, best-effort with graceful fallback (a failure only reverts to
# TODAY's behaviour — a same-group background job — never drops the sync):
#   1. `setsid` (Linux/util-linux): new session → fully detached. Preferred.
#   2. `nohup ... & disown` (macOS/BSD, no setsid): immune to SIGHUP and
#      removed from the shell's job table so it isn't reaped on shell exit.
#   3. plain `( ... ) &`: the pre-this-change behaviour, when neither is
#      available.
# $1 = the shell snippet to run (passed to `sh -c`).
# MUST MATCH the detach intent of _lib/kg-sync-debounce.ps1's Start-Process.
_kg_debounce_detach() {
    _snip="$1"
    if command -v setsid >/dev/null 2>&1; then
        setsid sh -c "$_snip" >/dev/null 2>&1 < /dev/null &
    elif command -v nohup >/dev/null 2>&1; then
        nohup sh -c "$_snip" >/dev/null 2>&1 < /dev/null &
        disown 2>/dev/null || true
    else
        ( sh -c "$_snip" ) >/dev/null 2>&1 < /dev/null &
    fi
}

# Build the snippet a detached IMMEDIATE-sync runs: cd into $1, eval $2.
# Both args are embedded via _kg_debounce_shquote so paths/cmds with spaces
# or quotes survive the extra `sh -c` layer.
#   $1 = working dir, $2 = sync command string
_kg_debounce_immediate_snip() {
    printf 'cd %s 2>/dev/null || true; eval %s' \
        "$(_kg_debounce_shquote "$1")" "$(_kg_debounce_shquote "$2")"
}

# Flusher body, runnable from a fresh detached shell that has re-sourced
# this lib (Item 1). Sleeps the quiet-window, then claims its own lock via
# the SAME atomic rename the reaper uses (lock → ".reaping.<token>") and
# recovers it through the shared _kg_debounce_run_claimed path so the sync
# re-reads the file fresh → latest content. The completion-time `mv` is the
# exactly-once gate: normally the flusher wins its rename and runs once; if
# its sleep stretched past window+GRACE a reaper may have already claimed +
# run the lock, in which case this `mv` fails (source gone) and it no-ops.
# This is the SAME logic the old inline `( sleep; mv; run_claimed ) &`
# subshell ran — factored into a function so the detached shell can call it
# instead of re-inlining (no divergence risk).
#   $1 = window seconds, $2 = lock dir, $3 = state dir, $4 = lock base name
_kg_debounce_flusher_body() {
    _fb_window="$1" _fb_lock="$2" _fb_dir="$3" _fb_base="$4"
    sleep "$_fb_window"
    _fb_won="$_fb_lock.reaping.$(_kg_debounce_claimtok)"
    if mv "$_fb_lock" "$_fb_won" 2>/dev/null; then
        _kg_debounce_run_claimed "$_fb_won" "$_fb_dir" "$_fb_base"
        # _kg_debounce_run_claimed backgrounds its own subshell; wait so this
        # flusher shell does not exit before the re-stamp + eval complete.
        wait
    fi
    # else: a reaper already claimed+ran this lock → no-op.
}

# Spawn the flusher DETACHED from the hook's process group (Item 1). The
# flusher sleeps the quiet-window, so it is the part most exposed to being
# killed if the hook's group is signalled mid-sleep. We re-source this lib
# in a fresh shell and call _kg_debounce_flusher_body there:
#   1. `setsid bash -c '...'` — new session, fully detached. Preferred.
#   2. `nohup bash -c '...' & disown` — SIGHUP-immune + off the job table,
#      for macOS/BSD where setsid may be absent.
#   3. plain `( _kg_debounce_flusher_body ... ) &` IN-PROCESS — today's
#      behaviour, when neither setsid nor a re-sourceable shell is available
#      (e.g. POSIX `sh` with no bash). A bug here only reverts to the
#      pre-this-change same-group background job; the sync is never dropped.
# A re-source needs an absolute lib path + a shell that can source it; when
# $_KG_DEBOUNCE_LIB isn't a readable file we use the in-process fallback so
# the flusher always runs.
#   $1 = window, $2 = lock dir, $3 = state dir, $4 = lock base name
_kg_debounce_spawn_flusher() {
    _sf_window="$1" _sf_lock="$2" _sf_dir="$3" _sf_base="$4"
    if [ -r "$_KG_DEBOUNCE_LIB" ] && command -v bash >/dev/null 2>&1; then
        _sf_snip="$(printf '. %s; _kg_debounce_flusher_body %s %s %s %s' \
            "$(_kg_debounce_shquote "$_KG_DEBOUNCE_LIB")" \
            "$(_kg_debounce_shquote "$_sf_window")" \
            "$(_kg_debounce_shquote "$_sf_lock")" \
            "$(_kg_debounce_shquote "$_sf_dir")" \
            "$(_kg_debounce_shquote "$_sf_base")")"
        if command -v setsid >/dev/null 2>&1; then
            setsid bash -c "$_sf_snip" >/dev/null 2>&1 < /dev/null &
            return 0
        elif command -v nohup >/dev/null 2>&1; then
            nohup bash -c "$_sf_snip" >/dev/null 2>&1 < /dev/null &
            disown 2>/dev/null || true
            return 0
        fi
    fi
    # Fallback: in-process backgrounded subshell (pre-Item-1 behaviour).
    ( _kg_debounce_flusher_body "$_sf_window" "$_sf_lock" "$_sf_dir" "$_sf_base" ) &
}

# Reap abandoned work. Two sweeps, both using the SAME atomic rename-claim
# so that for any one piece of pending work EXACTLY ONE process runs it:
#   (A) live ".lock" dirs older than window+GRACE → orphaned (the flusher
#       died mid-sleep, OR its sleep stretched past the grace budget while
#       still alive). Atomically claim via `mv "$lock" "$lock.claimed.$$"`;
#       only the winner runs the cmd. A concurrent reaper's `mv` fails
#       (source gone) and a live flusher that later wakes finds its own
#       lock renamed away — its completion-time `mv` (see the flusher in
#       _kg_debounce_schedule) also fails, so it too no-ops. Exactly one
#       run.
#   (B) stale ".claimed.<pid>" dirs whose owning <pid> is dead (a process
#       died between claim and eval). Re-claim via `mv` to our own pid and
#       run it. A live <pid> is a claim in flight → left untouched. This
#       is the residual-safety net for coalesce-NEVER-DROP.
# GRACE absorbs sleep scheduling jitter.
#   $1 = PROJECT_ROOT
_kg_debounce_reap_stale() {
    local proot="$1"
    local dir window grace now lock claimed age mtime cpid base won reclaimed
    local stamp last_reap reap_age
    dir="$(_kg_debounce_dir "$proot")"
    [ -d "$dir" ] || return 0
    window="$(_kg_debounce_window)"
    grace=$(( window + 30 ))   # window + 30s jitter budget
    now=$(date +%s 2>/dev/null) || return 0

    # Reaper throttle: skip the O(M^2) sweep if we ran it within the last
    # ~30s. Mirrors the diagram-throttle stamp-file pattern in
    # post-file-edit.sh (~line 336). A `.ts` stamp holds the epoch of the
    # last completed reap; we no-op if it's too recent. This is SAFE for
    # coalesce-NEVER-DROP: orphans are recovered by the very NEXT
    # un-throttled reap, at most ~throttle extra delay, still well inside
    # the eventually-consistent contract (orphans are already a rare crash
    # residual, not the steady-state path). Best-effort: a stat/write
    # failure falls through to running the reap (fail toward MORE recovery).
    stamp="$dir/.last_reap.ts"
    last_reap=0
    if [ -f "$stamp" ]; then
        last_reap=$(cat "$stamp" 2>/dev/null || printf '0')
        case "$last_reap" in ''|*[!0-9]*) last_reap=0 ;; esac
    fi
    reap_age=$(( now - last_reap ))
    if [ "$last_reap" -gt 0 ] && [ "$reap_age" -lt "$_KG_DEBOUNCE_REAP_THROTTLE_SECONDS" ]; then
        return 0
    fi
    # Stamp BEFORE sweeping so concurrent schedule calls during this sweep
    # also see the throttle (avoids a thundering-herd of parallel reaps).
    printf '%s' "$now" > "$stamp" 2>/dev/null || true

    # --- Sweep A: orphaned (or stretched-live) ".lock" dirs --------------
    for lock in "$dir"/*.lock; do
        # No-glob-match → literal pattern; skip.
        [ -e "$lock" ] || continue
        mtime=$(_kg_debounce_mtime "$lock" "$now")
        age=$(( now - mtime ))
        if [ "$age" -ge "$grace" ]; then
            # ATOMIC CLAIM: rename(2) within $dir is atomic; exactly one
            # racer's mv succeeds. The winner owns the orphan and runs it
            # exactly once; every loser (a second reaper, or the live
            # flusher waking to its completion-time mv) gets a non-zero mv
            # (source gone) and skips. This is the single point that makes
            # double-recovery impossible — replacing the old read-cmd-then-
            # rm order whose comment promised this but did not deliver it.
            base=$(basename "$lock")                 # e.g. "kg_<md5>.lock"
            won="$lock.reaping.$(_kg_debounce_claimtok)"
            if mv "$lock" "$won" 2>/dev/null; then
                # Refresh mtime to NOW: rename preserves the (backdated)
                # mtime, so without this a concurrent reaper's Sweep C
                # would immediately re-trip on this fresh ".reaping" dir
                # and double-run. Touch restarts the grace clock at the
                # claim transition. (run_claimed touches again after its
                # re-stamp, covering the whole in-flight lifetime.)
                touch "$won" 2>/dev/null || true
                # Hand the won dir to the runner, which re-stamps it with
                # its REAL backgrounded pid and runs the cmd once.
                _kg_debounce_run_claimed "$won" "$dir" "$base"
            fi
            # else: another process already claimed/removed it → skip.
        fi
    done

    # --- Sweep B: dead-pid ".claimed.<pid>" residuals --------------------
    # A process that died between winning a claim and finishing its eval
    # leaves a ".claimed.<pid>" dir (the runner re-stamped it with its real
    # pid). If <pid> is no longer alive the work was claimed-but-not-run →
    # recover it (re-claim, run). A still-alive <pid> is an in-flight claim
    # → leave it. We only sweep claims at least `grace` old so we never
    # race a just-created claim; combined with the kill -0 liveness check
    # this makes a false recovery require BOTH pid-reuse AND age —
    # vanishingly rare, and even then the result is at-most-one extra run,
    # never a drop.
    for claimed in "$dir"/*.claimed.*; do
        [ -e "$claimed" ] || continue
        # Extract the trailing numeric pid from "...claimed.<pid>".
        cpid="${claimed##*.claimed.}"
        case "$cpid" in ''|*[!0-9]*) continue ;; esac   # malformed → skip
        mtime=$(_kg_debounce_mtime "$claimed" "$now")
        age=$(( now - mtime ))
        [ "$age" -ge "$grace" ] || continue   # too fresh → leave in flight
        # If the owning pid is still alive, the claim is in flight → leave.
        if kill -0 "$cpid" 2>/dev/null; then
            continue
        fi
        # Dead owner → re-claim atomically and run. Rename to a fresh
        # ".reaping.<token>" first (so a second reaper's mv on the same
        # source fails → exactly one re-claimer), then hand to the runner
        # which re-stamps to its own real pid.
        base=$(printf '%s' "$(basename "$claimed")" | sed 's/\.claimed\.[0-9]*$//')  # strip ".claimed.<pid>"
        reclaimed="$dir/$base.reaping.$(_kg_debounce_claimtok)"
        if mv "$claimed" "$reclaimed" 2>/dev/null; then
            touch "$reclaimed" 2>/dev/null || true   # restart grace clock
            _kg_debounce_run_claimed "$reclaimed" "$dir" "$base"
        fi
    done

    # --- Sweep C: stranded ".reaping.<token>" residuals ------------------
    # A runner that died between winning the race-rename and the re-stamp
    # to ".claimed.<realpid>" leaves a ".reaping.<token>" dir. The reaping
    # window is microseconds (one rename), so any ".reaping" dir older than
    # `grace` is definitively stranded — its token carries no liveness info
    # (it is the CLAIMER's pid+nonce, not the runner's), so recover purely
    # on age. Re-claim with a fresh token (exactly-one re-claimer via the
    # atomic mv) and hand to the runner. This closes the last
    # claimed-but-not-run gap, preserving coalesce-NEVER-DROP.
    for claimed in "$dir"/*.reaping.*; do
        [ -e "$claimed" ] || continue
        mtime=$(_kg_debounce_mtime "$claimed" "$now")
        age=$(( now - mtime ))
        [ "$age" -ge "$grace" ] || continue   # too fresh → live re-stamp in flight
        base=$(printf '%s' "$(basename "$claimed")" | sed 's/\.reaping\..*$//')  # strip ".reaping.<token>"
        reclaimed="$dir/$base.reaping.$(_kg_debounce_claimtok)"
        if mv "$claimed" "$reclaimed" 2>/dev/null; then
            touch "$reclaimed" 2>/dev/null || true   # restart grace clock
            _kg_debounce_run_claimed "$reclaimed" "$dir" "$base"
        fi
    done
}

# Schedule a debounced sync for one file.
#   $1 = PROJECT_ROOT
#   $2 = file path (used only to derive the lock key)
#   $3 = python interpreter ($PY, for hashing)
#   $4 = working directory the sync command must run in
#   $5 = the sync command string (eval'd verbatim at flush time)
#   $6 = channel tag (e.g. "kg" / "docs" / "code") — namespaces the lock
#        so the SAME file routed to two different sync targets (e.g.
#        knowledge/foo.sh → both KG sync AND code-graph) gets two
#        independent debounce locks instead of one clobbering the other.
#
# Behaviour: if a flush is already pending for this (file, channel) →
# no-op (the pending flush will pick up the latest content). Otherwise
# atomically claim the lock + spawn a single `sleep N; sync` flusher.
# When N==0, runs the sync immediately in the background (debounce off).
_kg_debounce_schedule() {
    local proot="$1" fpath="$2" py="$3" wd="$4" cmd="$5" chan="${6:-kg}"
    local window dir key lock lock_count

    window="$(_kg_debounce_window)"

    # Item 3 (wd TAB/newline guard): the cmd-file line is persisted as
    # `printf '%s\t%s\n' "$wd" "$cmd"` and the reaper splits it on TAB +
    # reads only the FIRST line. A $wd containing a literal TAB or newline
    # would corrupt that record (TAB → wrong cmd split; newline → the cmd
    # truncated at head -1). $wd is always PROJECT_ROOT today (no embedded
    # control chars), so this can't fire now — but stripping them is free
    # defense-in-depth against any future caller that passes a hostile path.
    # tr -d (not reject): a sync into a slightly-wrong dir is still better
    # than dropping the sync, and the cmd itself re-derives paths from the
    # repo root at flush time.
    case "$wd" in
        *"$(printf '\t')"* | *"$(printf '\n')"*)
            wd="$(printf '%s' "$wd" | tr -d '\t\n')"
            ;;
    esac

    # Debounce disabled → preserve legacy "sync immediately" behaviour
    # (now detached so it survives the hook's process-group exit — Item 1).
    if [ "$window" = "0" ]; then
        _kg_debounce_detach "$(_kg_debounce_immediate_snip "$wd" "$cmd")"
        return 0
    fi

    # Recover any orphaned pending syncs before scheduling a new one.
    _kg_debounce_reap_stale "$proot"

    dir="$(_kg_debounce_dir "$proot")"
    mkdir -p "$dir" 2>/dev/null || {
        # Can't create state dir → fail OPEN to immediate (detached) sync so
        # a permission problem never silently drops the sync.
        _kg_debounce_detach "$(_kg_debounce_immediate_snip "$wd" "$cmd")"
        return 0
    }

    # Item 2a (flusher cap): if too many flushers are already pending
    # (one ".lock" dir each), stop ACCUMULATING sleeping flusher subshells
    # and instead sync THIS edit immediately in the (detached) background.
    # CRITICAL: this NEVER drops a sync — it only swaps "schedule a sleeping
    # flusher" for "run the sync now". The already-scheduled flushers still
    # sync their own files; this edit syncs immediately rather than adding a
    # 65th sleeper. (We count via a glob into a positional list — POSIX, no
    # `ls | wc` subshell quirks. A literal no-match leaves one bogus entry,
    # corrected below.)
    set -- "$dir"/*.lock
    if [ "$1" = "$dir/*.lock" ] && [ ! -e "$1" ]; then
        lock_count=0          # glob matched nothing
    else
        lock_count=$#
    fi
    if [ "$lock_count" -ge "$_KG_DEBOUNCE_LOCK_CEILING" ]; then
        _kg_debounce_detach "$(_kg_debounce_immediate_snip "$wd" "$cmd")"
        return 0
    fi

    key="${chan}_$(_kg_debounce_key "$fpath" "$py")"
    lock="$dir/$key.lock"

    # Atomic claim: mkdir succeeds for exactly one racer; the loser sees
    # a non-zero exit and treats the edit as coalesced into the pending
    # flush (no-op).
    if mkdir "$lock" 2>/dev/null; then
        # Record the command so a reaper can recover it if we die.
        printf '%s\t%s\n' "$wd" "$cmd" > "$lock/cmd" 2>/dev/null || true
        local base
        base="$(basename "$lock")"   # "<chan>_<md5>.lock"
        # Spawn the single flusher DETACHED from the hook's process group
        # (Item 1): it sleeps the quiet-window, then claims its own lock via
        # the SAME atomic rename the reaper uses and recovers it through the
        # shared _kg_debounce_run_claimed path, so the sync re-reads the file
        # fresh → latest content. The completion-time `mv` is the
        # exactly-once gate: normally (sleep finished within grace) the
        # flusher wins its own rename and runs the cmd once; if its sleep
        # stretched past window+GRACE a reaper may have already claimed+run
        # the lock, in which case the flusher's `mv` fails (source gone) and
        # it does NOT double-run. The flusher's normal completion and the
        # reaper's recovery are the SAME mechanism, making "exactly once"
        # structural rather than comment-promised. Detaching (setsid →
        # nohup+disown → in-process fallback) keeps the sleeping flusher
        # alive even if the hook's process group is signalled mid-sleep,
        # mirroring the PowerShell sibling's Start-Process detach.
        _kg_debounce_spawn_flusher "$window" "$lock" "$dir" "$base"
    fi
    # else: lock already held → a flush is pending → no-op. Correct: the
    # pending flusher will sync the latest file content.
    # Explicit success return: the caller invokes this as a bare statement
    # under `set -e`, so the function must never propagate a non-zero exit
    # (e.g. from a failed mkdir on the lock-held path) and kill the hook.
    return 0
}
