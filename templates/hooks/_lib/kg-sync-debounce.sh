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
#      N + GRACE seconds is treated as abandoned — we delete it and run
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

# Hash a file path → a slash-free lock key. Reuses the portable
# md5-via-Python pattern already used by the diagram throttle in
# post-file-edit.sh. $1 = path, $2 = python interpreter ($PY).
_kg_debounce_key() {
    printf '%s' "$1" \
        | "$2" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" \
        2>/dev/null || printf '%s' "_"
}

# Reap abandoned locks (flusher died mid-sleep). Any lock older than
# window+GRACE is treated as orphaned: delete it and run its recorded
# sync NOW (backgrounded), so the file is never left permanently
# un-synced. GRACE absorbs sleep scheduling jitter.
#   $1 = PROJECT_ROOT
_kg_debounce_reap_stale() {
    local proot="$1"
    local dir window grace now lock cmd_file age mtime _reap_line
    dir="$(_kg_debounce_dir "$proot")"
    [ -d "$dir" ] || return 0
    window="$(_kg_debounce_window)"
    grace=$(( window + 30 ))   # window + 30s jitter budget
    now=$(date +%s 2>/dev/null) || return 0

    for lock in "$dir"/*.lock; do
        # No-glob-match → literal pattern; skip.
        [ -e "$lock" ] || continue
        # mtime of the lock dir. `stat` flag differs across coreutils
        # (GNU -c %Y) vs BSD/macOS (-f %m); try both, then give up
        # safely (treat as fresh → do not reap).
        mtime=$(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || echo "$now")
        case "$mtime" in ''|*[!0-9]*) mtime="$now" ;; esac
        age=$(( now - mtime ))
        if [ "$age" -ge "$grace" ]; then
            cmd_file="$lock/cmd"
            # Capture the recorded command BEFORE deleting the lock — the
            # cmd file lives INSIDE the lock dir, so `rm -rf` would erase
            # it first and lose the sync. The recorded line is: working
            # dir, a TAB, then the command to eval.
            _reap_line=""
            [ -f "$cmd_file" ] && _reap_line=$(head -1 "$cmd_file" 2>/dev/null || echo "")
            # Remove the lock so a concurrent reaper can't double-run.
            rm -rf "$lock" 2>/dev/null || true
            if [ -n "$_reap_line" ]; then
                # Run backgrounded so reaping never blocks the live edit.
                (
                    _wd=$(printf '%s' "$_reap_line" | cut -f1)
                    _cmd=$(printf '%s' "$_reap_line" | cut -f2-)
                    [ -n "$_wd" ] && cd "$_wd" 2>/dev/null || true
                    [ -n "$_cmd" ] && eval "$_cmd" >/dev/null 2>&1 || true
                ) &
            fi
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
    local window dir key lock

    window="$(_kg_debounce_window)"

    # Debounce disabled → preserve legacy "sync immediately" behaviour.
    if [ "$window" = "0" ]; then
        ( cd "$wd" 2>/dev/null || true; eval "$cmd" ) &
        return 0
    fi

    # Recover any orphaned pending syncs before scheduling a new one.
    _kg_debounce_reap_stale "$proot"

    dir="$(_kg_debounce_dir "$proot")"
    mkdir -p "$dir" 2>/dev/null || {
        # Can't create state dir → fail OPEN to the legacy path so a
        # permission problem never silently drops the sync.
        ( cd "$wd" 2>/dev/null || true; eval "$cmd" ) &
        return 0
    }
    key="${chan}_$(_kg_debounce_key "$fpath" "$py")"
    lock="$dir/$key.lock"

    # Atomic claim: mkdir succeeds for exactly one racer; the loser sees
    # a non-zero exit and treats the edit as coalesced into the pending
    # flush (no-op).
    if mkdir "$lock" 2>/dev/null; then
        # Record the command so a reaper can recover it if we die.
        printf '%s\t%s\n' "$wd" "$cmd" > "$lock/cmd" 2>/dev/null || true
        # Spawn the single flusher. It sleeps the quiet-window, then
        # removes the lock (so subsequent edits re-arm) and runs the
        # sync, which re-reads the file fresh → latest content.
        (
            sleep "$window"
            rm -rf "$lock" 2>/dev/null || true
            cd "$wd" 2>/dev/null || true
            eval "$cmd" >/dev/null 2>&1 || true
        ) &
    fi
    # else: lock already held → a flush is pending → no-op. Correct: the
    # pending flusher will sync the latest file content.
    # Explicit success return: the caller invokes this as a bare statement
    # under `set -e`, so the function must never propagate a non-zero exit
    # (e.g. from a failed mkdir on the lock-held path) and kill the hook.
    return 0
}
