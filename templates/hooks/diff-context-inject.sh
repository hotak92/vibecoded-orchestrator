#!/usr/bin/env bash
# Diff-based context injection — only inject CHANGED sections of CONTEXT_STATE.md
# First prompt: create baseline snapshot (full injection handled by session-start hooks)
# Subsequent prompts: output only changed sections (or nothing if unchanged)
# After /compact: reset baseline (detected via compact flag)

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably — bare `python3` is missing on Windows
# (only `python.exe`/`py` exist there); fallback chain: python3 → python → py.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"

# Hook input contract (v2.1.x): session_id arrives as JSON on stdin, not as
# the $CLAUDE_SESSION_ID env var (which Claude Code does NOT populate —
# confirmed empirically 2026-05-08). Reading the env var meant every session
# in this project shared the same `default` snapshot file, so two concurrent
# sessions silently stomped on each other's diff baseline.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
if [ -n "${PY:-}" ]; then
    SESSION_ID=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', '') or 'default')
except Exception:
    print('default')
" 2>/dev/null || echo "default")
else
    SESSION_ID="default"
fi
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# V52-J Edit 4 (2026-06-09): export VCT_SESSION_ID so child processes
# inherit the session_id. The canonical telemetry emit path
# (claude_mcp_servers/rl_client/telemetry_emit.py::resolve_session_id)
# reads VCT_SESSION_ID as layer-2 of its 3-layer chain. Skip the
# "default" sentinel — we'd rather have empty than fake-key. This hook
# itself does not spawn telemetry-emitting subprocesses, but it fires
# on UserPromptSubmit (early in every turn) so exporting here primes
# the env for any later in-turn shell-pipeline call (search_knowledge
# CLI from a snippet, etc.).
if [ -n "$SESSION_ID" ] && [ "$SESSION_ID" != "default" ]; then
    export VCT_SESSION_ID="$SESSION_ID"
fi

CONTEXT_FILE=".claude/CONTEXT_STATE.md"
# Snapshot state lives under the project (gitignored .claude/state/) so it
# survives reboots and launcher restarts — Claude Code's `resume` feature
# can reuse a session_id across these boundaries, and a $TMPDIR-based path
# would lose the diff baseline mid-session when /tmp is wiped on boot.
# The `ctx_` filename prefix namespaces these files within the shared
# .claude/state/ dir (which also holds seen_kg_titles_*, reads_*, etc.).
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
SNAPSHOT_DIR="$PROJECT_DIR/.claude/state"
SNAPSHOT_FILE="$SNAPSHOT_DIR/ctx_snapshot_${SESSION_ID}"
COMPACT_FLAG="$SNAPSHOT_DIR/ctx_compact_flag_${SESSION_ID}"

# Track C (v0.2.65): per-session CONTEXT_STATE file. Concurrent long-lived
# chats against the same project (e.g. a main chat + an RL chat) each keep
# their own session_id and would otherwise clobber one shared
# CONTEXT_STATE.md (last-writer-wins). If a session writes its own
# .claude/context/CONTEXT_STATE_<session_id>.md, we diff it independently
# using a SECOND snapshot baseline keyed on `ctx_snapshot_session_`. The
# shared CONTEXT_STATE.md rollup is untouched. Zero cost when the
# per-session file is absent (single-session projects never create it).
SESSION_CONTEXT_FILE="$PROJECT_DIR/.claude/context/CONTEXT_STATE_${SESSION_ID}.md"
SESSION_SNAPSHOT_FILE="$SNAPSHOT_DIR/ctx_snapshot_session_${SESSION_ID}"

mkdir -p "$SNAPSHOT_DIR"

# 14-day GC for stale ctx_snapshot_* files — sessions that haven't fired
# in two weeks are stale enough that their baseline is no longer useful.
# Best-effort (no error if find misses files). Doesn't touch the compact
# flags (those are short-lived sentinels, cleaned by post-compact.sh).
# The `ctx_snapshot_*` glob also matches the Track C per-session baseline
# `ctx_snapshot_session_*`; both are throwaway diff baselines, so the same
# 14-day sweep applies. It NEVER touches the per-session CONTEXT_STATE
# content files under .claude/context/ — those may be user-curated.
find "$SNAPSHOT_DIR" -maxdepth 1 -type f -name "ctx_snapshot_*" -mtime +14 -delete 2>/dev/null || true

# If compact flag exists, reset baseline. Also reset the Track C per-session
# baseline so the post-compact view recomputes from the current file rather
# than emitting an incoherent diff against the pre-compact baseline.
if [ -f "$COMPACT_FLAG" ]; then
    rm -f "$COMPACT_FLAG" "$SNAPSHOT_FILE" "$SESSION_SNAPSHOT_FILE"
fi

# diff_context_section: emit the changed ## sections of $1 (the live file)
# relative to $2 (its snapshot baseline), then refresh the baseline. $3 is a
# short label used only in the emitted header. Reused for both the shared
# CONTEXT_STATE.md and the Track C per-session file — one diff implementation,
# two invocations (no duplicated diff code; see CLAUDE.md "one concern, one
# home"). Returns 0 always; soft-fails on any read error.
diff_context_section() {
    local live_file="$1"
    local snap_file="$2"
    local label="$3"

    # If the live file doesn't exist, nothing to do.
    [ -f "$live_file" ] || return 0

    # If no snapshot exists, create baseline (first prompt — full injection
    # is done by the session-start hooks).
    if [ ! -f "$snap_file" ]; then
        cp "$live_file" "$snap_file"
        return 0
    fi

    # Quick check: if files are identical, nothing to do.
    if cmp -s "$live_file" "$snap_file"; then
        return 0
    fi

    # Files differ — find which ## sections changed using diff.
    # Get line numbers from the NEW file. We MUST also suppress old-side line
    # content (default --old-line-format is "%l\n"), otherwise content of
    # removed lines leaks into $changed_lines and the integer test below
    # floods stderr with "integer expression expected". Captured uncapped into
    # JSONL hook attachments, that produced 14-23 MB stderr blobs and
    # triggered the 2026-05-07 GUI freeze (Bug B / main-thread streaming stall).
    local changed_lines
    changed_lines=$(diff --unchanged-line-format='' --old-line-format='' --new-line-format='%dn ' "$snap_file" "$live_file" 2>/dev/null || true)

    if [ -z "$changed_lines" ]; then
        # diff found differences but no new lines — file got shorter.
        # Output a note and update snapshot.
        echo "[${label} updated — sections removed]"
        cp "$live_file" "$snap_file"
        return 0
    fi

    # Find which ## sections contain the changed lines.
    local changed_sections=""
    local current_section=""
    local line_num=0
    local line cl
    while IFS= read -r line; do
        line_num=$((line_num + 1))
        if [[ "$line" =~ ^##\  ]]; then
            current_section="$line"
        fi
        # Check if this line number is in our changed set.
        for cl in $changed_lines; do
            if [ "$line_num" -eq "$cl" ] && [ -n "$current_section" ]; then
                # Add to changed sections (dedup later).
                if [[ "$changed_sections" != *"$current_section"* ]]; then
                    changed_sections="${changed_sections}${current_section}"$'\n'
                fi
                break
            fi
        done
    done < "$live_file"

    # Output changed sections.
    if [ -n "$changed_sections" ]; then
        echo "[${label} update — changed sections:]"
        echo ""
        local section_header
        while IFS= read -r section_header; do
            [ -z "$section_header" ] && continue
            # Extract this section from current file (header to next ## or EOF).
            awk -v header="$section_header" '
                $0 == header { found=1 }
                found && /^## / && $0 != header { exit }
                found { print }
            ' "$live_file"
            echo ""
        done <<< "$changed_sections"
    fi

    # Update snapshot.
    cp "$live_file" "$snap_file"
    return 0
}

# 1. The shared CONTEXT_STATE.md rollup (the original, unchanged behaviour).
diff_context_section "$CONTEXT_FILE" "$SNAPSHOT_FILE" "Context"

# 2. Track C: the per-session CONTEXT_STATE file, IF it exists. A second
# invocation against the `_session_` baseline — gated entirely on file
# existence, so projects that never write a per-session file are unaffected.
if [ -f "$SESSION_CONTEXT_FILE" ]; then
    diff_context_section "$SESSION_CONTEXT_FILE" "$SESSION_SNAPSHOT_FILE" "Session context"
fi

exit 0
