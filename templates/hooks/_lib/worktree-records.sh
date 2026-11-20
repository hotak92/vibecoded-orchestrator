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
#                             {name, mode: head|snapshot|in_place|reservation|
#                              reservation_existing, path, base, toplevel,
#                              status: created, created_at}
#                             A lane-reservation launch (v0.2.102 W6-AA,
#                             vco_lib/lane_reservation.py) adds reservation_id,
#                             repo, branch, base_ref, base_sha, branch_created;
#                             its `toplevel` is the TARGET repo R, so the reap
#                             below runs `git -C R`, never against the project.
#   subagent-stop-reconcile   NEVER removes a worktree (v0.2.107 REAP-INTERIM):
#                             a provably unchanged one becomes a reap
#                             CANDIDATE (wtrec_reap → status reap_candidate),
#                             a changed one is kept; outcome in `status`
#   session-worktree-reap     SessionStart sweep (wtrec_sweep): removes a
#                             candidate idle for WTREC_ABANDON_DAYS and a kept
#                             worktree whose commits are merged into the main
#                             checkout's branch, re-checking every condition
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

# wtrec_write PROJECT_ROOT NAME MODE PATH BASE TOPLEVEL [EXTRA_JSON]
# EXTRA_JSON (v0.2.102 W6-AA): an optional one-line JSON object of STRING
# fields merged into the record — a lane reservation's binding
# (reservation_id, repo, branch, base_ref, base_sha, branch_created). The
# fixed fields above always win over an extra key of the same name.
wtrec_write() {
    local root="$1" name="$2"
    wtrec_valid_name "$name" || return 0
    [ -n "${PY:-}" ] || return 0
    mkdir -p "$root/$WTREC_SUBDIR" 2>/dev/null || return 0
    WTREC_OUT="$root/$WTREC_SUBDIR/wt-$name.json" WTREC_NAME="$name" WTREC_MODE="$3" \
    WTREC_PATH="$4" WTREC_BASE="$5" WTREC_TOP="$6" WTREC_EXTRA="${7:-}" "$PY" -c '
import json, os
from datetime import datetime, timezone
e = os.environ
rec = {}
try:
    extra = json.loads(e.get("WTREC_EXTRA") or "{}")
except ValueError:
    extra = {}
if isinstance(extra, dict):
    rec.update({str(k): v for k, v in extra.items() if isinstance(v, str)})
rec.update({"name": e["WTREC_NAME"], "mode": e["WTREC_MODE"], "path": e["WTREC_PATH"],
       "base": e["WTREC_BASE"], "toplevel": e["WTREC_TOP"], "status": "created",
       "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
with open(e["WTREC_OUT"], "w", encoding="utf-8") as fh:
    json.dump(rec, fh)
' 2>/dev/null || true
}

# wtrec_find PROJECT_ROOT AGENT_ID — print the record file for this agent
# (`wt-agent-<id>.json`, else `wt-<id>.json`); return 1 when there is none.
# A found record's mtime is bumped (`touch -c`), so the mtime tracks the
# agent's LAST activity (a resume's SubagentStart, its SubagentStop) and
# wtrec_prune's idle window counts from there, not from the launch.
wtrec_find() {
    local root="$1" id="$2" f
    wtrec_valid_name "$id" || return 1
    for f in "$root/$WTREC_SUBDIR/wt-agent-$id.json" "$root/$WTREC_SUBDIR/wt-$id.json"; do
        if [ -f "$f" ]; then
            touch -c "$f" 2>/dev/null || true
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
# resumed agent): wtrec_prune keeps every record until it has been idle
# longer than the resume window, and each lookup here (wtrec_find) restarts
# that idle clock.
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

# wtrec_set FILE STATUS REASON [SESSION_ID [TRANSCRIPT]] — record the reap
# outcome on the record. With SESSION_ID (a SubagentStop decision) the
# session and transcript of that stop are stored too (reap_session,
# reap_transcript): wtrec_sweep never touches a worktree of the session that
# is starting, and it needs to know which session that is for a KEPT record
# as much as for a candidate.
wtrec_set() {
    [ -n "${PY:-}" ] || return 0
    WTREC_FILE="$1" WTREC_STATUS="$2" WTREC_REASON="$3" WTREC_HAS_SID="${4+yes}" \
    WTREC_SID="${4:-}" WTREC_TPATH="${5:-}" "$PY" -c '
import json, os
from datetime import datetime, timezone
e = os.environ
p = e["WTREC_FILE"]
try:
    with open(p, encoding="utf-8") as fh:
        rec = json.load(fh)
    rec["status"] = e["WTREC_STATUS"]
    rec["status_reason"] = e["WTREC_REASON"]
    rec["status_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if e.get("WTREC_HAS_SID") == "yes":
        rec["reap_session"] = e.get("WTREC_SID", "")
        rec["reap_transcript"] = e.get("WTREC_TPATH", "")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
except Exception:
    pass
' 2>/dev/null || true
}

# wtrec_kept_alert_once RECORD_FILE SESSION_ID WORKTREE_PATH — 0 when the
# PARENT should be told "worktree KEPT" now, 1 when it was already told about
# THIS path in THIS session.
#
# WHY (v0.2.105): a subagent that starts its own background work (a test
# suite, a build) stops and resumes several times, and every interim stop
# fires SubagentStop. The harness payload carries no field that tells an
# interim stop from a final one — SubagentStop input is session_id,
# transcript_path, cwd, permission_mode, hook_event_name, stop_hook_active,
# agent_id, agent_type, agent_transcript_path, last_assistant_message,
# effort, background_tasks and session_crons
# (https://code.claude.com/docs/en/hooks#subagentstop-input), and
# background_tasks / session_crons are scoped to the PARENT session, not the
# subagent (effort does not separate the two either) — so a first-stop notice
# was read as "the agent finished". The answer is one honest notice per
# worktree path per session, worded so a reader knows the agent may still be
# running (the call site owns that wording).
#
# WHICH GATE DECIDES: in the ordinary sequence the SECOND stop of one launch
# whose worktree was KEPT is already suppressed by the PRE-EXISTING
# `already_handled` gate in wtrec_reap (the first stop flipped `status` to
# `kept`) before this function is ever called; a reap CANDIDATE gets no
# notice at all. This marker is the belt to that suspender: it
# covers the rewritten-record case (a record re-made re-reapable that still
# carries kept_alert_*) and the detached-job race (stop 2's job reaps before
# stop 1's job has written its status). Both directions fail OPEN — a
# missing or unreadable marker emits. The marker rides the agent's own
# record, so wtrec_prune's idle window retires it with the record; a record
# rewritten for a NEW path (a fresh launch) alerts again.
#
# Best-effort and FAIL-OPEN: no python, an unreadable record, a valid-JSON
# non-object record or a failed write means "emit" — a real kept worktree is
# never reported silently and a corrupt record is never rewritten.
# MUST MATCH Test-WtrecKeptAlertOnce in the .ps1.
wtrec_kept_alert_once() {
    local rec="$1" sid="${2:-}" path="$3"
    [ -n "$rec" ] && [ -f "$rec" ] || return 0
    [ -n "$path" ] || return 0
    [ -n "${PY:-}" ] || return 0
    WTREC_FILE="$rec" WTREC_SID="$sid" WTREC_PATH="$path" "$PY" -c '
import json, os, sys
p = os.environ["WTREC_FILE"]
sid = os.environ.get("WTREC_SID", "")
path = os.environ.get("WTREC_PATH", "")
try:
    with open(p, encoding="utf-8") as fh:
        rec = json.load(fh)
except Exception:
    print("emit")
    sys.exit(0)
if not isinstance(rec, dict):
    print("emit")
    sys.exit(0)
if rec.get("kept_alert_path") == path and str(rec.get("kept_alert_session", "")) == sid:
    print("seen")
    sys.exit(0)
rec["kept_alert_path"] = path
rec["kept_alert_session"] = sid
try:
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, p)
except OSError:
    print("emit")
    sys.exit(0)
print("emit")
' 2>/dev/null | grep -qx seen && return 1
    return 0
}

# wtrec_prune PROJECT_ROOT — keep the directory bounded without breaking a
# resume. A record outlives its worktree on purpose: a resumed subagent's
# SubagentStart must still find it (isolation-check), or the skipped-isolation
# warning and the Layer 3b worktree-violation alert fire for an agent the
# guard DID isolate. So:
#   * notices (one-shot, consumed at SubagentStart) go after 30 days;
#   * records of ANY status go only once IDLE (mtime — wtrec_find bumps it on
#     every lookup) longer than max(30, cleanupPeriodDays) + 30 days. A
#     `wt-*.json.tmp` (or the .ps1 sibling's `.json.bak`) left by a crash
#     between wtrec_kept_alert_once's tmp write and its rename ages out on
#     that same window (nothing reads it — every lookup is by exact name — so
#     this is only hygiene).
#     `cleanupPeriodDays` is Claude Code's transcript retention (default 30):
#     a subagent can be resumed while its transcript exists, and the extra 30
#     days is margin. It is the ONLY key read, from
#     ${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json and the project's
#     .claude/settings.json + settings.local.json; the largest valid value
#     wins (a longer window only keeps records longer). A bool, negative or
#     non-number value is ignored. Nothing else in those files is used or
#     printed.
#   * no python → no record is pruned (the window cannot be computed; a
#     too-early prune is the defect, a few extra small files are not).
# MUST MATCH Remove-WtrecStale in _lib/worktree-records.ps1.
wtrec_prune() {
    local root="$1" d="$1/$WTREC_SUBDIR"
    [ -d "$d" ] || return 0
    find "$d" -maxdepth 1 -type f -name 'notice-*.txt' -mtime +30 \
        -exec rm -f {} + 2>/dev/null || true
    [ -n "${PY:-}" ] || return 0
    WTREC_DIR="$d" WTREC_ROOT="$root" "$PY" -c '
import json, math, os, time
e = os.environ
cfg = e.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
root = e["WTREC_ROOT"]
days = 30.0
for p in (os.path.join(cfg, "settings.json"),
          os.path.join(root, ".claude", "settings.json"),
          os.path.join(root, ".claude", "settings.local.json")):
    try:
        with open(p, encoding="utf-8-sig") as fh:
            v = json.load(fh).get("cleanupPeriodDays")
    except Exception:
        continue
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        continue
    try:
        fv = float(v)
    except OverflowError:
        continue  # beyond a double: invalid (PowerShell cannot parse it either)
    if math.isnan(fv) or math.isinf(fv) or fv < 0:
        continue
    days = max(days, fv)
cut = time.time() - (days + 30.0) * 86400.0
d = e["WTREC_DIR"]
for n in os.listdir(d):
    # `.json.tmp` is marker-write crash residue; `.json.bak` belongs to the .ps1
    # sibling (File.Replace needs a backup path), pruned here too so the two
    # prune rules stay identical.
    if not (n.startswith("wt-") and (n.endswith(".json")
                                      or n.endswith(".json.tmp")
                                      or n.endswith(".json.bak"))):
        continue
    p = os.path.join(d, n)
    try:
        if os.path.isfile(p) and not os.path.islink(p) and os.path.getmtime(p) < cut:
            os.remove(p)
    except OSError:
        pass
' 2>/dev/null || true
}

# wtrec_changed_files PROJECT_ROOT AGENT_ID — v0.2.102 W6-C2 (census F7):
# print, one ABSOLUTE path per line, the files the agent changed in ITS OWN
# worktree: uncommitted (modified/added/untracked) plus everything committed
# since the recorded base. Attribution is exact here — nobody else writes in
# that worktree — unlike the shared-tree snapshot diff, which also sees the
# parent's and sibling lanes' edits. Prints nothing when the agent has no
# record or its worktree is gone (reaped = unchanged). Read-only.
# MUST MATCH Get-WtrecChangedFiles in the .ps1.
wtrec_changed_files() {
    local rec path base
    rec="$(wtrec_find "$1" "$2")" || return 0
    path="$(wtrec_get "$rec" path)"
    base="$(wtrec_get "$rec" base)"
    [ -n "$path" ] && [ -d "$path" ] || return 0
    # NUL-separated (-z): porcelain QUOTES a path with a space or a special
    # character even under core.quotepath=off, which made `<wt>/"a b.py"`
    # (review R4c-7). With -z every entry is `XY <path>`, a rename/copy
    # (R/C) followed by its ORIGIN path as the next entry (skipped).
    {
        git -C "$path" status --porcelain -z --untracked-files=all 2>/dev/null | {
            _wtrec_skip=0
            while IFS= read -r -d '' _wtrec_e; do
                if [ "$_wtrec_skip" = 1 ]; then _wtrec_skip=0; continue; fi
                case "${_wtrec_e:0:1}" in R|C) _wtrec_skip=1 ;; esac
                [ "${#_wtrec_e}" -gt 3 ] && printf '%s\0' "${_wtrec_e:3}"
            done
        }
        if [ -n "$base" ]; then
            git -C "$path" diff --name-only -z "$base" HEAD 2>/dev/null
        fi
    } | sort -zu | while IFS= read -r -d '' _wtrec_rel; do
        [ -n "$_wtrec_rel" ] && printf '%s/%s\n' "$path" "$_wtrec_rel"
    done
}

# _wtrec_rmdir_lanes_parent TOPLEVEL PATH — after the reap removed a lane
# reservation's worktree, remove its now-EMPTY parent `<R>.vco-lanes/`
# (vco_lib/lane_reservation.py's path convention: sibling of R's toplevel).
# rmdir only: never recursive, so a directory still holding another lane's
# worktree or any stray file is left exactly as it is. Only that exact
# directory is ever touched. MUST MATCH Remove-WtrecLanesParent in the .ps1.
# Both paths are normalised LEXICALLY first (_wtrec_lexnorm: `.`, `..`,
# repeated and trailing separators; no symlink resolution), the same way the
# .ps1 normalises them with GetFullPath + TrimEnd (v0.2.102 W6-LOWS, R3 AA-5).
_wtrec_rmdir_lanes_parent() {
    local top="$1" path="$2" parent lanes
    [ -n "$top" ] && [ -n "$path" ] || return 0
    top="$(_wtrec_lexnorm "$top")"
    path="$(_wtrec_lexnorm "$path")"
    parent="$(dirname "$path")"
    lanes="$(dirname "$top")/$(basename "$top").vco-lanes"
    lanes="$(_wtrec_lexnorm "$lanes")"
    [ "$parent" = "$lanes" ] || return 0
    rmdir "$lanes" 2>/dev/null || true
}

# _wtrec_lexnorm PATH — print PATH made absolute (against $PWD) and normalised
# lexically: empty and `.` segments dropped, `..` pops the previous segment
# (never above `/`), no trailing separator. Like .NET's GetFullPath on POSIX,
# it does NOT resolve symlinks. MUST MATCH the $norm block of
# Remove-WtrecLanesParent in the .ps1.
_wtrec_lexnorm() {
    local p="$1" rest seg
    local -a stack=()
    case "$p" in /*) ;; *) p="$PWD/$p" ;; esac
    rest="$p"
    while [ -n "$rest" ]; do
        seg="${rest%%/*}"
        if [ "$seg" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
        case "$seg" in
            ''|.) ;;
            ..) [ "${#stack[@]}" -gt 0 ] && unset "stack[$(( ${#stack[@]} - 1 ))]" ;;
            *) stack+=("$seg") ;;
        esac
    done
    if [ "${#stack[@]}" -eq 0 ]; then printf '/\n'; else printf '/%s' "${stack[@]}"; printf '\n'; fi
}

# ── Reaping (v0.2.107 REAP-INTERIM, owner rulings 2026-11-20) ───────────────
# WHY TWO PHASES: SubagentStop also fires on an INTERIM stop — the agent ends
# its turn waiting on its own background work or a lock and resumes later —
# and its payload cannot tell interim from final (see wtrec_kept_alert_once
# for the field list). Removing the worktree there removed it under a paused
# agent, and SendMessage then could not resume it ("its worktree no longer
# exists"). So:
#   1. SubagentStop (wtrec_reap) never removes anything: a provably unchanged
#      worktree is marked `status: reap_candidate`, a changed one `kept`, and
#      either way the record stores the PARENT session id (reap_session) and
#      the transcript path from the payload (reap_transcript); a candidate
#      also gets the marking time (reap_marked_epoch). A later stop of the
#      same agent re-checks a candidate (it may have written since) and turns
#      it into `kept` when it has.
#   2. wtrec_sweep, run at SessionStart by session-worktree-reap, removes
#      (a) an UNCHANGED candidate once its record and transcript have been
#          idle for WTREC_ABANDON_DAYS ("keep longer": an ended chat alone is
#          not enough — the owner may still resume it);
#      (b) a KEPT worktree whose HEAD moved past its base and is now an
#          ancestor of the MAIN checkout's current branch HEAD, whose
#          `git status --porcelain --untracked-files=all` is empty (ignored
#          files do not count), together with the branch VCO created for it
#          (`git branch -d`, which refuses an unmerged branch on its own).
#      Never a worktree of the session that is starting, a record with no
#      session recorded, an in_place folder or an adopted
#      (reservation_existing) worktree. Every condition is re-checked right
#      before `git worktree remove` (never --force), and ANY doubt keeps the
#      worktree: a failing git command, an unknown main branch (detached or
#      unborn), a record that does not match the worktree (path not
#      registered, wrong branch), a `git worktree lock`, or a process whose
#      cwd is inside the worktree — or no way to tell (_wtrec_in_use).

# Idle days after which an unchanged candidate is removed. MUST MATCH
# $script:WtrecAbandonDays in the .ps1.
WTREC_ABANDON_DAYS=7

# wtrec_reap PROJECT_ROOT AGENT_ID [SESSION_ID] [TRANSCRIPT] — SubagentStop's
# step: decide the agent's worktree WITHOUT removing it. Prints one line:
#   "<decision>\t<reason>\t<path>"  decision ∈ candidate|removed|kept|skipped
# (`removed` only as `already_gone`: the worktree no longer exists) and prints
# nothing when the agent has no record (the guard never ran).
wtrec_reap() {
    local rec
    rec="$(wtrec_find "$1" "$2")" || return 0
    _wtrec_reap_rec "$rec" candidate "${3:-}" "${4:-}"
}

# _wtrec_wt_block LIST PATH — print the `git worktree list --porcelain` block
# (LIST) of the worktree at exactly PATH; return 1 when PATH is not in it.
_wtrec_wt_block() {
    printf '%s\n' "$1" | awk -v p="worktree $2" '
        $0 == p { on = 1; found = 1 }
        on && $0 == "" { on = 0 }
        on { print }
        END { exit found ? 0 : 1 }'
}

# _wtrec_in_use PATH — print `yes` when a process has its cwd at or under
# PATH, `no` when none has, `unknown` when that cannot be told: Linux reads
# /proc/<pid>/cwd, macOS asks `lsof -d cwd`, anything else (Windows) is
# unknown. A process whose cwd cannot be read (another user's) is skipped.
# MUST MATCH Test-WtrecInUse in the .ps1.
_wtrec_in_use() {
    [ -n "${PY:-}" ] || { echo unknown; return 0; }
    WTREC_PATH="$1" "$PY" -c '
import os, subprocess, sys
p = os.path.normpath(os.path.abspath(os.environ["WTREC_PATH"]))
want = {p, os.path.realpath(p)}
def inside(c):
    c = os.path.normpath(c)
    return any(c == w or c.startswith(w.rstrip("/") + "/") for w in want)
cwds = []
if sys.platform.startswith("linux") and os.path.isdir("/proc/self"):
    for n in os.listdir("/proc"):
        if not n.isdigit():
            continue
        try:
            c = os.readlink("/proc/" + n + "/cwd")
        except OSError:
            continue
        if c.endswith(" (deleted)"):
            c = c[:-10]
        cwds.append(c)
elif sys.platform == "darwin":
    try:
        r = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn"], capture_output=True,
                           text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        print("unknown")
        sys.exit(0)
    if r.returncode != 0:
        print("unknown")
        sys.exit(0)
    cwds = [ln[1:] for ln in r.stdout.splitlines() if ln.startswith("n")]
else:
    print("unknown")
    sys.exit(0)
print("yes" if any(inside(c) for c in cwds) else "no")
' 2>/dev/null || echo unknown
}

# _wtrec_reap_rec RECORD_FILE ACTION [SESSION_ID] [TRANSCRIPT] — the one
# decision for an UNCHANGED worktree. ACTION `candidate` (SubagentStop) marks
# it a reap candidate; ACTION `remove` (wtrec_sweep, rule a) removes it.
# Removed (or marked) ONLY when ALL hold (conservative — any doubt keeps):
#   * mode is head, snapshot or reservation (in_place = the user's own
#     folder, reservation_existing = a worktree the coordinator made: NEVER);
#   * status is `created` or `reap_candidate` (candidate) / `reap_candidate`
#     (remove) — `kept` / `removed` are final: already_handled;
#   * the path exists, is a registered worktree of the record's toplevel and
#     is not that toplevel;
#   * HEAD equals the recorded base (so no commits beyond it, and no switch);
#   * `git status --porcelain --untracked-files=all --ignored=matching` is
#     empty — nothing modified, nothing untracked, and no IGNORED file either
#     (a fresh checkout has none, so any is the agent's output, e.g. a report
#     under the git-excluded `.claude/state/`);
#   * (remove) the worktree is not locked and no process has its cwd inside
#     it — otherwise the decision is `deferred` (the record stays a candidate
#     and the next sweep asks again) — and `git worktree remove` (no --force)
#     agrees.
# worktree-guard creates DETACHED worktrees for head/snapshot, so there is no
# branch to delete; a reservation worktree's new branch is deleted with it
# (compare-and-delete against the base, above), and the lanes directory it
# lived in goes too once it is empty (_wtrec_rmdir_lanes_parent). A snapshot
# base commit is on no ref once the worktree is gone; git's normal gc
# collects it.
_wtrec_reap_rec() {
    local rec="$1" action="$2" sid="${3:-}" tpath="${4:-}" mode path base top head st err listed block status
    mode="$(wtrec_get "$rec" mode)"
    path="$(wtrec_get "$rec" path)"
    base="$(wtrec_get "$rec" base)"
    top="$(wtrec_get "$rec" toplevel)"
    _wtrec_out() {
        printf '%s\t%s\t%s\n' "$1" "$2" "$path"
        case "$1" in
            skipped|deferred) ;;
            *) if [ "$action" = "candidate" ]; then
                   wtrec_set "$rec" "$1" "$2" "$sid" "$tpath"
               else
                   wtrec_set "$rec" "$1" "$2"
               fi ;;
        esac
    }
    case "$mode" in
        head|snapshot|reservation) ;;
        in_place) _wtrec_out skipped in_place_never_removed; return 0 ;;
        reservation_existing) _wtrec_out skipped reservation_existing_never_removed; return 0 ;;
        *) _wtrec_out skipped unknown_mode; return 0 ;;
    esac
    status="$(wtrec_get "$rec" status)"
    case "$action:$status" in
        candidate:created|candidate:reap_candidate|remove:reap_candidate) ;;
        *) _wtrec_out skipped already_handled; return 0 ;;
    esac
    if [ -z "$path" ] || [ -z "$base" ] || [ -z "$top" ] || [ "$path" = "$top" ]; then
        _wtrec_out kept incomplete_record
        return 0
    fi
    if [ ! -d "$path" ]; then
        _wtrec_out removed already_gone
        [ "$mode" = "reservation" ] && _wtrec_rmdir_lanes_parent "$top" "$path"
        return 0
    fi
    listed="$(git -C "$top" worktree list --porcelain 2>/dev/null)"
    if ! block="$(_wtrec_wt_block "$listed" "$path")"; then
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
    if [ "$action" = "candidate" ]; then
        printf '%s\t%s\t%s\n' candidate unchanged_reap_deferred "$path"
        _wtrec_mark_candidate "$rec" "$sid" "$tpath"
        return 0
    fi
    if printf '%s\n' "$block" | grep -q '^locked'; then
        _wtrec_out deferred locked
        return 0
    fi
    case "$(_wtrec_in_use "$path")" in
        no) ;;
        yes) _wtrec_out deferred in_use; return 0 ;;
        *) _wtrec_out deferred in_use_unknown; return 0 ;;
    esac
    if err="$(git -C "$top" worktree remove "$path" 2>&1)"; then
        # A lane reservation's worktree (mode reservation) was created on a
        # NEW branch (`worktree add -b`); remove that branch too, but only
        # while it still points at the recorded base (`update-ref -d <ref>
        # <old>` is git's compare-and-delete), so a branch that gained work
        # elsewhere is never lost.
        if [ "$mode" = "reservation" ] && [ "$(wtrec_get "$rec" branch_created)" = "yes" ]; then
            local br
            br="$(wtrec_get "$rec" branch)"
            if [ -n "$br" ] && git -C "$top" update-ref -d "refs/heads/$br" "$base" >/dev/null 2>&1; then
                _wtrec_out removed clean
            else
                _wtrec_out removed clean_branch_kept
            fi
            _wtrec_rmdir_lanes_parent "$top" "$path"
            return 0
        fi
        _wtrec_out removed clean
    else
        _wtrec_out kept "git_refused: $(printf '%s' "$err" | tr '\t\n' '  ' | cut -c1-200)"
    fi
}

# _wtrec_reap_merged RECORD_FILE — wtrec_sweep's rule (b): remove a KEPT
# worktree whose work is merged into the main checkout's current branch.
# Prints "<decision>\t<reason>\t<path>" when it removed the worktree
# (removed merged / merged_branch_kept) or when a merged worktree is held
# back by a doubt (deferred <reason>); prints NOTHING when the worktree is
# simply not eligible (wrong mode or status, gone, unchanged — rule a's
# business — or not merged yet), so a kept lane does not log a line at every
# session start. Never writes the record unless it removed the worktree.
# MUST MATCH Invoke-WtrecReapMerged in the .ps1.
_wtrec_reap_merged() {
    local rec="$1" mode path base top status listed main block head mref mhead rc st br err reason
    mode="$(wtrec_get "$rec" mode)"
    case "$mode" in head|snapshot|reservation) ;; *) return 0 ;; esac
    status="$(wtrec_get "$rec" status)"
    [ "$status" = "kept" ] || return 0
    path="$(wtrec_get "$rec" path)"
    base="$(wtrec_get "$rec" base)"
    top="$(wtrec_get "$rec" toplevel)"
    [ -n "$path" ] && [ -n "$base" ] && [ -n "$top" ] && [ "$path" != "$top" ] || return 0
    [ -d "$path" ] || return 0
    _wtrec_defer() { printf '%s\t%s\t%s\n' deferred "$1" "$path"; }
    head="$(git -C "$path" rev-parse --verify -q HEAD 2>/dev/null)" || return 0
    [ -n "$head" ] && [ "$head" != "$base" ] || return 0
    if ! listed="$(git -C "$top" worktree list --porcelain 2>/dev/null)"; then
        _wtrec_defer worktree_list_failed; return 0
    fi
    # The MAIN checkout is the first entry git lists (the main worktree).
    main="$(printf '%s\n' "$listed" | sed -n '1s/^worktree //p')"
    [ -n "$main" ] || { _wtrec_defer main_checkout_unknown; return 0; }
    [ "$main" != "$path" ] || return 0
    mref="$(git -C "$main" symbolic-ref -q HEAD 2>/dev/null)" || { _wtrec_defer main_branch_unknown; return 0; }
    mhead="$(git -C "$main" rev-parse --verify -q "$mref^{commit}" 2>/dev/null)" || {
        _wtrec_defer main_branch_unknown; return 0; }
    git -C "$main" merge-base --is-ancestor "$head" "$mhead" >/dev/null 2>&1
    rc=$?
    [ "$rc" = 1 ] && return 0
    [ "$rc" = 0 ] || { _wtrec_defer ancestry_unknown; return 0; }
    # ── merged: every doubt below keeps it ──
    block="$(_wtrec_wt_block "$listed" "$path")" || { _wtrec_defer not_a_registered_worktree; return 0; }
    br=""
    if [ "$mode" = "reservation" ]; then
        br="$(wtrec_get "$rec" branch)"
        [ -n "$br" ] && printf '%s\n' "$block" | grep -qxF "branch refs/heads/$br" || {
            _wtrec_defer record_mismatch; return 0; }
    else
        printf '%s\n' "$block" | grep -qx detached || { _wtrec_defer record_mismatch; return 0; }
    fi
    if printf '%s\n' "$block" | grep -q '^locked'; then _wtrec_defer locked; return 0; fi
    if ! st="$(git -C "$path" status --porcelain --untracked-files=all 2>/dev/null)"; then
        _wtrec_defer status_unreadable; return 0
    fi
    [ -z "$st" ] || { _wtrec_defer has_changes; return 0; }
    # Re-check right before the removal: HEAD unchanged since the ancestry test.
    [ "$(git -C "$path" rev-parse --verify -q HEAD 2>/dev/null)" = "$head" ] || {
        _wtrec_defer head_moved; return 0; }
    case "$(_wtrec_in_use "$path")" in
        no) ;;
        yes) _wtrec_defer in_use; return 0 ;;
        *) _wtrec_defer in_use_unknown; return 0 ;;
    esac
    if ! err="$(git -C "$top" worktree remove "$path" 2>&1)"; then
        _wtrec_defer "git_refused: $(printf '%s' "$err" | tr '\t\n' '  ' | cut -c1-200)"
        return 0
    fi
    reason=merged
    if [ "$mode" = "reservation" ]; then
        # Only the branch VCO created for the lane, and only through
        # `git branch -d` in the main checkout (refuses an unmerged branch).
        if [ "$(wtrec_get "$rec" branch_created)" != "yes" ] || case "$br" in -*) true ;; *) false ;; esac \
                || ! git -C "$main" branch -d "$br" >/dev/null 2>&1; then
            reason=merged_branch_kept
        fi
        _wtrec_rmdir_lanes_parent "$top" "$path"
    fi
    printf '%s\t%s\t%s\n' removed "$reason" "$path"
    wtrec_set "$rec" removed "$reason"
}

# _wtrec_mark_candidate RECORD_FILE SESSION_ID TRANSCRIPT — status
# reap_candidate plus what wtrec_sweep needs to decide when it may go.
# A non-object or unreadable record is left untouched (never swept then).
_wtrec_mark_candidate() {
    [ -n "${PY:-}" ] || return 0
    WTREC_FILE="$1" WTREC_SID="$2" WTREC_TPATH="$3" "$PY" -c '
import json, os, time
from datetime import datetime, timezone
e = os.environ
p = e["WTREC_FILE"]
try:
    with open(p, encoding="utf-8") as fh:
        rec = json.load(fh)
    if not isinstance(rec, dict):
        raise ValueError("not an object")
    rec["status"] = "reap_candidate"
    rec["status_reason"] = "unchanged_reap_deferred"
    rec["status_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec["reap_session"] = e.get("WTREC_SID", "")
    rec["reap_transcript"] = e.get("WTREC_TPATH", "")
    rec["reap_marked_epoch"] = int(time.time())
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
except Exception:
    pass
' 2>/dev/null || true
}

# wtrec_sweep PROJECT_ROOT CURRENT_SESSION_ID — run the two sweep rules (see
# "Reaping" above) over every record whose stop session is known and is not
# the session that is starting. Prints one line per decision:
# "<decision>\t<reason>\t<path>\t<record name>".
# No python → nothing is swept (eligibility cannot be read).
# MUST MATCH Invoke-WtrecSweep in the .ps1.
wtrec_sweep() {
    local root="$1" d="$1/$WTREC_SUBDIR" f rule
    [ -d "$d" ] || return 0
    [ -n "${PY:-}" ] || return 0
    WTREC_DIR="$d" WTREC_SID="${2:-}" WTREC_DAYS="$WTREC_ABANDON_DAYS" "$PY" -c '
import json, os, time
e = os.environ
d = e["WTREC_DIR"]
cur = e.get("WTREC_SID", "")
window = float(e["WTREC_DAYS"]) * 86400.0
now = time.time()
for n in sorted(os.listdir(d)):
    if not (n.startswith("wt-") and n.endswith(".json")):
        continue
    p = os.path.join(d, n)
    try:
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as fh:
            rec = json.load(fh)
        if not isinstance(rec, dict):
            continue
        sid = rec.get("reap_session")
        if not isinstance(sid, str) or not sid or sid == cur:
            continue
        status = rec.get("status")
        if status == "kept":
            print("merged\t" + p)
        elif status == "reap_candidate":
            last = os.path.getmtime(p)
            t = rec.get("reap_transcript")
            if isinstance(t, str) and t:
                try:
                    last = max(last, os.path.getmtime(t))
                except OSError:
                    pass
            if now - last >= window:
                print("remove\t" + p)
    except Exception:
        continue
' 2>/dev/null | while IFS="$(printf '\t')" read -r rule f; do
        [ -n "$f" ] || continue
        if [ "$rule" = "remove" ]; then
            _wtrec_reap_rec "$f" remove
        else
            _wtrec_reap_merged "$f"
        fi | while IFS= read -r line; do
            printf '%s\t%s\n' "$line" "$(basename "$f" .json | sed 's/^wt-//')"
        done
    done
}

# wtrec_log_reap LOG_FILE HOOK AGENT_ID DECISION REASON PATH — one row in
# .claude/logs/worktree-guard.jsonl for a reap decision (both phases).
# MUST MATCH Write-WtrecReapLog in the .ps1.
wtrec_log_reap() {
    [ -n "${PY:-}" ] || return 0
    mkdir -p "$(dirname "$1")" 2>/dev/null || true
    WTREC_LOG="$1" WTREC_HOOK="$2" WTREC_AGENT="$3" WTREC_DEC="$4" WTREC_REASON="$5" \
    WTREC_PATH="$6" "$PY" -c '
import json, os
from datetime import datetime, timezone
e = os.environ
row = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
       "hook": e["WTREC_HOOK"], "decision": "reap_" + e["WTREC_DEC"],
       "reason": e["WTREC_REASON"], "agent_id": e["WTREC_AGENT"],
       "resolved_path": e["WTREC_PATH"]}
with open(e["WTREC_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row) + "\n")
' 2>/dev/null || true
}
