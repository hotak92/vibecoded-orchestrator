# shellcheck shell=bash
# _lib/agent-alerts.sh - v0.2.102 W6-C2 fix round (review R4c-2/R4c-3).
#
# File one alert a SUBAGENT hit, for delivery to its PARENT
# (agent-alerts-deliver). The path, the row format and the append/claim
# locking live in ONE place, vco_lib/agent_alerts.py; this helper only hands
# it the row's inputs as JSON on stdin (`python -m vco_lib.agent_alerts
# append`). Hooks used to hand-build the row and append with `>>`, which
# opened the file before the interpreter wrote: a delivery that claimed the
# file in that window lost the row.
#
# vco_agent_alert_append <script_dir> <project_root> <session_id> <agent_id> \
#                        <agent_type> <kind> <message> [file]
#   Returns 0 when filed (or nothing to file: no agent id), 1 when the alert
#   could NOT be filed. Never silent on failure: the VCO venv is part of every
#   healthy install, so an unresolvable venv is a BROKEN install and is said
#   so on stderr; callers add their own louder channel (the model envelope,
#   a desktop notification).
#
# Sourced, never executed. MUST MATCH _lib/agent-alerts.ps1.

vco_agent_alert_append() {
    local sdir="${1:-}" proot="${2:-}" sid="${3:-}" agent="${4:-}" atype="${5:-}"
    local kind="${6:-}" msg="${7:-}" file="${8:-}" venv row
    [ -n "$agent" ] || return 0
    if ! command -v resolve_vco_venv_python >/dev/null 2>&1; then
        # shellcheck source=resolve-vco-venv.sh disable=SC1091
        [ -f "$sdir/_lib/resolve-vco-venv.sh" ] && . "$sdir/_lib/resolve-vco-venv.sh"
    fi
    VCO_VENV_PYTHON=""
    if command -v resolve_vco_venv_python >/dev/null 2>&1; then
        resolve_vco_venv_python "$sdir"
    fi
    venv="${VCO_VENV_PYTHON:-}"
    if [ -z "$venv" ] || [ ! -x "$venv" ]; then
        echo "agent-alerts: VCO venv not resolvable - this subagent alert was NOT filed for the parent (broken VCO install; repair it with the orchestrator update): $msg" >&2
        return 1
    fi
    row="$(A_SID="$sid" A_AGENT="$agent" A_TYPE="$atype" A_KIND="$kind" A_MSG="$msg" A_FILE="$file" \
        "$venv" -c '
import json, os
e = os.environ
row = {"session_id": e.get("A_SID", ""), "agent_id": e.get("A_AGENT", ""),
       "agent_type": e.get("A_TYPE", ""), "kind": e.get("A_KIND", ""),
       "message": e.get("A_MSG", "")}
if e.get("A_FILE"):
    row["file"] = e["A_FILE"]
print(json.dumps(row))
' 2>/dev/null)"
    if [ -z "$row" ] || ! printf '%s' "$row" | "$venv" -m vco_lib.agent_alerts append --project-root "$proot"; then
        echo "agent-alerts: filing this subagent alert for the parent FAILED: $msg" >&2
        return 1
    fi
    return 0
}
