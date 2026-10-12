# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# worktree-records.sh — the one home for worktree-guard's per-agent records
# (v0.2.102 W6-Q follow-up). MUST MATCH _lib/worktree-records.ps1.
#
# WHY
# ---
# VCO's WorktreeCreate hook (worktree-guard) creates every isolated agent's
# worktree, and the harness then "keeps the worktree your WorktreeCreate hook
# made for it and doesn't run your WorktreeRemove hook" when a subagent
# finishes (https://code.claude.com/docs/en/hooks#worktreeremove). Without a
# reaper, every isolated launch left a worktree behind, changed or not. Three
# hooks share one record per launch:
#
#   worktree-guard            writes  .claude/state/worktree-guard/wt-<name>.json
#                             {name, mode: head|snapshot|in_place, path, base,
#                              toplevel, status: created, created_at}
#   subagent-stop-reconcile   reaps the worktree when it is provably unchanged
#                             (wtrec_reap) and records the outcome in `status`
#   subagent-start-isolation-check
#                             asks whether the guard handled this agent at all
#                             (wtrec_isolation_skipped) to detect a skipped
#                             isolation; subagent-stop-reconcile asks the same
#                             question, plus the record's mode, to decide its
#                             post-hoc worktree-violation alert (Layer 3b)
#
# `name` is the WorktreeCreate identifier, `agent-<agent_id>` on the live
# harness (Claude Code 2.1.285); lookups by agent_id try both spellings.
# Identifiers outside [A-Za-z0-9._-] (or starting with '.') get no record —
# and so are never reaped.
#
# Every function is best-effort: no python, an unreadable record or a git
# failure means "do nothing". Requires $PY for JSON (callers resolve it).

WTREC_SUBDIR=".claude/state/worktree-guard"

# wtrec_valid_name NAME — 0 when NAME is safe as a file-name component.
wtrec_valid_name() {
    case "${1:-}" in
        ''|*[!A-Za-z0-9._-]*|.*) return 1 ;;
    esac
    return 0
}

# wtrec_write PROJECT_ROOT NAME MODE PATH BASE TOPLEVEL
wtrec_write() {
    local root="$1" name="$2"
    wtrec_valid_name "$name" || return 0
    [ -n "${PY:-}" ] || return 0
    mkdir -p "$root/$WTREC_SUBDIR" 2>/dev/null || return 0
    WTREC_OUT="$root/$WTREC_SUBDIR/wt-$name.json" WTREC_NAME="$name" WTREC_MODE="$3" \
    WTREC_PATH="$4" WTREC_BASE="$5" WTREC_TOP="$6" "$PY" -c '
import json, os
from datetime import datetime, timezone
e = os.environ
rec = {"name": e["WTREC_NAME"], "mode": e["WTREC_MODE"], "path": e["WTREC_PATH"],
       "base": e["WTREC_BASE"], "toplevel": e["WTREC_TOP"], "status": "created",
       "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
with open(e["WTREC_OUT"], "w", encoding="utf-8") as fh:
    json.dump(rec, fh)
' 2>/dev/null || true
}

# wtrec_find PROJECT_ROOT AGENT_ID — print the record file for this agent
# (`wt-agent-<id>.json`, else `wt-<id>.json`); return 1 when there is none.
wtrec_find() {
    local root="$1" id="$2" f
    wtrec_valid_name "$id" || return 1
    for f in "$root/$WTREC_SUBDIR/wt-agent-$id.json" "$root/$WTREC_SUBDIR/wt-$id.json"; do
        if [ -f "$f" ]; then
            printf '%s\n' "$f"
            return 0
        fi
    done
    return 1
}

# wtrec_agent_def_isolation PROJECT_ROOT AGENT_TYPE — print the `isolation:`
# frontmatter value of the agent definition whose `name` (else file stem) is
# AGENT_TYPE, searched in <project>/.claude/agents then ~/.claude/agents.
# Prints "" when the definition is unknown or declares no isolation.
wtrec_agent_def_isolation() {
    [ -n "${PY:-}" ] || return 0
    AGENT_TYPE_FOR_PY="$2" PROJECT_FOR_PY="$1" "$PY" -c '
import os, re, sys
from pathlib import Path
want = os.environ["AGENT_TYPE_FOR_PY"]
roots = [Path(os.environ["PROJECT_FOR_PY"]) / ".claude" / "agents",
         Path.home() / ".claude" / "agents"]
def front(p):
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip("\"'\''")
    return out
for root in roots:
    if not root.is_dir():
        continue
    for p in sorted(root.rglob("*.md")):
        fm = front(p)
        if fm is None:
            continue
        if fm.get("name", p.stem) == want:
            print(fm.get("isolation", ""))
            sys.exit(0)
' 2>/dev/null || true
}

# wtrec_guard_registered PROJECT_ROOT — 0 when worktree-guard is registered in
# the project's settings. When it is parked, the harness makes the worktree
# itself and the guard writes no record, so "no record" proves nothing.
wtrec_guard_registered() {
    local f
    for f in "$1/.claude/settings.json" "$1/.claude/settings.local.json"; do
        [ -f "$f" ] && grep -q 'worktree-guard\.' "$f" 2>/dev/null && return 0
    done
    return 1
}

# wtrec_isolation_skipped PROJECT_ROOT AGENT_ID AGENT_TYPE — 0 when it is
# DECIDABLE that this launch skipped its isolation: the definition (found,
# and not a plugin-scoped `a:b` type) declares `isolation: worktree`,
# worktree-guard is registered, and the guard wrote no record for this agent
# id. Anything unknown returns 1 — never guess an agent into a violation. A
# record that outlived its reaped worktree still counts as isolated (a
# resumed agent).
wtrec_isolation_skipped() {
    local root="$1" id="$2" type="$3"
    case "$type" in ''|*:*) return 1 ;; esac
    wtrec_valid_name "$id" || return 1
    wtrec_find "$root" "$id" >/dev/null && return 1
    wtrec_guard_registered "$root" || return 1
    [ "$(wtrec_agent_def_isolation "$root" "$type")" = "worktree" ]
}

# wtrec_get FILE KEY — print one string field of a record ("" when absent).
wtrec_get() {
    [ -n "${PY:-}" ] || return 0
    WTREC_FILE="$1" WTREC_KEY="$2" "$PY" -c '
import json, os
try:
    with open(os.environ["WTREC_FILE"], encoding="utf-8") as fh:
        v = json.load(fh).get(os.environ["WTREC_KEY"], "")
except Exception:
    v = ""
print(v if isinstance(v, str) else "")
' 2>/dev/null || true
}

# wtrec_set FILE STATUS REASON — record the reap outcome on the record.
wtrec_set() {
    [ -n "${PY:-}" ] || return 0
    WTREC_FILE="$1" WTREC_STATUS="$2" WTREC_REASON="$3" "$PY" -c '
import json, os
from datetime import datetime, timezone
p = os.environ["WTREC_FILE"]
try:
    with open(p, encoding="utf-8") as fh:
        rec = json.load(fh)
    rec["status"] = os.environ["WTREC_STATUS"]
    rec["status_reason"] = os.environ["WTREC_REASON"]
    rec["status_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
except Exception:
    pass
' 2>/dev/null || true
}

# wtrec_prune PROJECT_ROOT — drop records and notices older than 30 days, so
# the directory stays bounded. A record outlives its worktree on purpose: a
# resumed subagent's SubagentStart must still find it (isolation-check).
wtrec_prune() {
    local d="$1/$WTREC_SUBDIR"
    [ -d "$d" ] || return 0
    find "$d" -maxdepth 1 -type f \( -name 'wt-*.json' -o -name 'notice-*.txt' \) -mtime +30 \
        -exec rm -f {} + 2>/dev/null || true
}

# wtrec_reap PROJECT_ROOT AGENT_ID — remove the agent's worktree when it is
# provably unchanged; keep it otherwise. Prints one line:
#   "<decision>\t<reason>\t<path>"  decision ∈ removed|kept|skipped
# and prints nothing when the agent has no record (the guard never ran).
#
# Removed ONLY when ALL hold (conservative — any doubt keeps the worktree):
#   * mode is head or snapshot (in_place = the user's own folder: NEVER);
#   * the path exists, is a registered worktree of the record's toplevel and
#     is not that toplevel;
#   * HEAD equals the recorded base (so no commits beyond it, and no switch);
#   * `git status --porcelain --untracked-files=all --ignored=matching` is
#     empty — nothing modified, nothing untracked, and no IGNORED file either
#     (a fresh checkout has none, so any is the agent's output, e.g. a report
#     under a git-excluded `.claude/`);
#   * `git worktree remove` (no --force) agrees — git refuses an unclean or
#     locked worktree on its own.
# worktree-guard always creates DETACHED worktrees, so there is no branch to
# delete. A snapshot base commit is on no ref once the worktree is gone;
# git's normal gc collects it.
wtrec_reap() {
    local root="$1" id="$2" rec mode path base top head st err listed
    rec="$(wtrec_find "$root" "$id")" || return 0
    mode="$(wtrec_get "$rec" mode)"
    path="$(wtrec_get "$rec" path)"
    base="$(wtrec_get "$rec" base)"
    top="$(wtrec_get "$rec" toplevel)"
    _wtrec_out() {
        printf '%s\t%s\t%s\n' "$1" "$2" "$path"
        [ "$1" = "skipped" ] || wtrec_set "$rec" "$1" "$2"
    }
    case "$mode" in
        head|snapshot) ;;
        in_place) _wtrec_out skipped in_place_never_removed; return 0 ;;
        *) _wtrec_out skipped unknown_mode; return 0 ;;
    esac
    if [ "$(wtrec_get "$rec" status)" != "created" ]; then
        _wtrec_out skipped already_handled
        return 0
    fi
    if [ -z "$path" ] || [ -z "$base" ] || [ -z "$top" ] || [ "$path" = "$top" ]; then
        _wtrec_out kept incomplete_record
        return 0
    fi
    if [ ! -d "$path" ]; then
        _wtrec_out removed already_gone
        return 0
    fi
    listed="$(git -C "$top" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')"
    if ! printf '%s\n' "$listed" | grep -qxF "$path"; then
        _wtrec_out kept not_a_registered_worktree
        return 0
    fi
    head="$(git -C "$path" rev-parse HEAD 2>/dev/null || true)"
    if [ "$head" != "$base" ]; then
        _wtrec_out kept head_moved_or_commits
        return 0
    fi
    if ! st="$(git -C "$path" status --porcelain --untracked-files=all --ignored=matching 2>/dev/null)"; then
        _wtrec_out kept status_unreadable
        return 0
    fi
    if [ -n "$st" ]; then
        _wtrec_out kept has_changes
        return 0
    fi
    if err="$(git -C "$top" worktree remove "$path" 2>&1)"; then
        _wtrec_out removed clean
    else
        _wtrec_out kept "git_refused: $(printf '%s' "$err" | tr '\t\n' '  ' | cut -c1-200)"
    fi
}
