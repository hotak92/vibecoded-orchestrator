# shellcheck shell=bash
# _lib/pid-live.sh -- "is the process that stamped this record still alive?"
#
# One home (v0.2.104) for the pid-reuse-safe liveness check, extracted from
# _lib/kg-sync-debounce.sh (where it was born as `_kg_debounce_pid_live`,
# v0.2.103 HIGH-3) when pre-tool-use gained a second caller: its in-progress
# markers must tell a sibling hook run that is STILL running from one the
# harness killed. kg-sync-debounce.sh keeps its old name as a thin wrapper.
#
# v0.2.103 PID-REUSE GUARD (HIGH-3, review R3 2026-10-14). `kill -0` alone
# answers "does SOME process have this pid?" -- but Linux pid space is small
# and wraps, Windows recycles pids fast in a narrow range, so a record left
# by a DEAD process whose pid was later handed to an unrelated long-lived
# process used to look "alive" FOREVER.
# A pid now counts as the record's live owner only if the process ALSO
# started at/before the record's stamp (every caller stamps AFTER its owner
# process started, so a genuine owner always satisfies this; a recycled pid
# never does, because it started after the stamping process died).
# Read-only, never kills anything. Soft-fail policy, decided on evidence:
# when the process exists but its START TIME cannot be read we keep the
# pre-guard semantics (treat as live). Every supported platform can read a
# start time (Linux /proc/<pid>/stat, macOS/BSD `ps`, Windows Get-Process in
# the .ps1 sibling), so the unreadable branch is a broken-install residual;
# both callers prefer "live" when unsure (a debounce claim mis-read as dead
# resurrects duplicate syncs; a pre-tool-use marker mis-read as dead warns
# the model about a sibling call that is still being checked).
# MUST MATCH Test-VcoPidLive in _lib/pid-live.ps1.
#   $1 = pid, $2 = stamp epoch (whole seconds)
VCO_PID_LIVE_SLACK_SECS=5
# Tolerance for mtime coarseness / scheduling jitter between the stamp and
# the process-start reading. A genuine owner's start is within milliseconds
# of the stamp; a recycled pid is seconds-to-years later -- 5s separates the
# two by orders of magnitude. MUST MATCH the .ps1 sibling.
vco_pid_live() {
    local pid="$1" before="$2"
    local limit btime hz start start_epoch et now t v d
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    case "$before" in ''|*[!0-9]*) return 0 ;; esac   # no stamp -> old semantics
    kill -0 "$pid" 2>/dev/null || return 1
    limit=$(( before + VCO_PID_LIVE_SLACK_SECS ))
    # Linux fast path: /proc/<pid>/stat field 22 (starttime, clock ticks
    # since boot) + /proc/stat btime -> absolute epoch of the process start.
    # No external process spawn, and readable for every user's processes.
    if [ -r "/proc/$pid/stat" ]; then
        btime=$(sed -n 's/^btime //p' /proc/stat 2>/dev/null)
        hz=$(getconf CLK_TCK 2>/dev/null || printf '100')
        case "$btime$hz" in ''|*[!0-9]*) return 0 ;; esac
        # comm may contain spaces/parens; field 2 is "(...)" -- strip through
        # the LAST ')' (the greedy `^.*\)` match; no later field can hold a
        # ')') so field N of the remainder is stat field N+2 (starttime is
        # field 22 -> remainder field 20). Stripping through the FIRST ')'
        # mis-splits a comm like "a) b" and reads the wrong field.
        start=$(awk '{ r = $0; sub(/^.*\)/, "", r); n = split(r, f, " "); if (n >= 20) print f[20] }' "/proc/$pid/stat" 2>/dev/null)
        case "$start" in ''|*[!0-9]*) return 0 ;; esac
        start_epoch=$(( btime + start / hz ))
        [ "$start_epoch" -le "$limit" ] && return 0
        return 1
    fi
    # ps fallback (macOS / BSD): elapsed seconds. procps-style `ps -o etimes=`
    # prints seconds directly; macOS ps has no etimes but does have `etime`
    # ("[[dd-]hh:]mm:ss"), parsed below. Process start = now - elapsed.
    et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d '[:space:]')
    case "$et" in
        *[!0-9]*|'') ;;
        *)
            now=$(date +%s 2>/dev/null || printf '0')
            case "$now" in *[!0-9]*) return 0 ;; esac
            [ $(( now - et )) -le "$limit" ] && return 0
            return 1
            ;;
    esac
    t=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d '[:space:]')
    v=0
    case "$t" in
        *[!0-9:-]*) return 0 ;;                 # unparseable -> soft-fail live
        '')
            return 0
            ;;
    esac
    d=0
    case "$t" in
        *-*) d=$(( 10#${t%%-*} * 86400 )); t="${t#*-}" ;;
    esac
    while :; do
        case "$t" in
            *:*) v=$(( v * 60 + 10#${t%%:*} )); t="${t#*:}" ;;
            *)  v=$(( v * 60 + 10#$t )); break ;;
        esac
    done
    # 10#: an etime field like "08" is decimal, never a bash octal error.
    v=$(( v + d ))
    now=$(date +%s 2>/dev/null || printf '0')
    case "$now" in *[!0-9]*) return 0 ;; esac
    [ $(( now - v )) -le "$limit" ] && return 0
    return 1
}
