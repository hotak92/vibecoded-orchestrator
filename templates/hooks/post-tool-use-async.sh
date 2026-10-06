#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# post-tool-use-async.sh — the ONE async PostToolUse dispatcher (v0.2.101).
#
# WHY THIS HOOK EXISTS (transcript-bloat fix, measured metadata-only):
# Pre-v0.2.101 the settings templates carried EIGHT async PostToolUse
# registrations across six scripts (post-edit-outcome, post-bash-context-
# record, kg-summary-generator x3, post-git-commit-kg-sync, post-file-
# delete, kg-update-nudge). One Bash tool call spawned up to 3 async
# processes, and every async run that SPOKE (any stdout/stderr) or DIED
# (timeout) wrote an `async_hook_response` attachment (~660 B) into the
# session transcript. One maintainer transcript held 700,330 such records /
# 462.3 MB. This dispatcher replaces those eight registrations with ONE
# (matcher `*`, async, timeout 15): 3 spawns -> 1 per tool call, <= 1
# transcript record per tool call and only when the dispatcher itself is
# killed — because it guarantees silence (below).
#
# WHAT IT DOES
#   1. Reads the hook stdin ONCE into a 0600 temp file (mktemp default).
#   2. Parses the routing fields (tool_name; whether tool_input.command
#      starts with "git commit") with the same find-python + inline-Python
#      idiom every sub-hook uses — no second parsing mechanism.
#   3. Routes over the ROUTE_TABLE below: tool equality or `*`, with the
#      `git-commit-prefix` gate carrying the retired registration key
#      `if: Bash(git commit *)` (word-boundary: `git commit` exactly or
#      followed by a space — `git commit-tree` does NOT fire the review
#      agent). The `if: Edit|Write(knowledge/**/*.md)` keys the retired
#      kg-summary-generator registrations carried are enforced by that
#      script's OWN knowledge-path validation (unchanged body — one home
#      preserved; this file is a router, not a re-implementation. That
#      delegation costs one short-lived no-op python per non-knowledge
#      Edit/Write — review N-4, kept deliberately: any dispatcher-side
#      pre-filter would be a SECOND home of the knowledge-path rule, and
#      the spawn is concurrent and off the critical path). A stem listed
#      in VCO_ASYNC_DISABLED_HOOKS (<project>/.claude/env — the launcher's
#      Hooks-tab sub-hook toggle) is skipped (review SF-2).
#   4. Runs every matched sub-hook CONCURRENTLY (background children +
#      wait) — the wall-time shape the separate async registrations had;
#      the single 15 s registration timeout bounds the fan-out (the
#      largest retired per-hook budget was 10 s).
#   5. GUARANTEES SILENCE: its own stdout/stderr are redirected away from
#      the harness before any work (`exec` below); child stdout is
#      discarded; child stderr and non-zero exits condense to ONE line per
#      failure in `<VCO metrics dir>/post-tool-use-async.log`
#      (`_lib/metrics-dir.sh` resolves the home). Always exits 0 —
#      PostToolUse cannot block, and any output would be transcript bloat
#      again.
#
# BEHAVIOUR PRESERVED PER SUB-HOOK: each script still ships unchanged,
# receives the exact stdin bytes the harness gave (temp-file redirect),
# keeps its own internal gates (tool_name checks, knowledge-path
# validation, VCT_DISABLE_HOOKS guards) and its own internal exits; env
# (VCT_KG_ACCESS_LIST, CLAUDE_PROJECT_DIR, ...) is inherited exactly as it
# was when the harness spawned each script directly.
#
# The retired registrations are declared in vco_lib/hook_retirements.py
# (event PostToolUse, retired_in v0.2.101) so EXISTING installs lose them
# at the next bundle update; the nudge's SYNC UserPromptSubmit +
# SessionStart(compact) registrations are event-scoped out of that and
# stay alive.
#
# VCO-CENTRALIZED-KG: router (PR #171 / 0.1.7 classification). Touches no
#   Weaviate collection itself; routes PostToolUse payloads to the
#   classified scripts (kg-summary-generator = write-side delegator,
#   kg-update-nudge = counter-only, post-git-commit-kg-sync = spawns
#   claude). The access matrix (VCT_KG_ACCESS_LIST /
#   VCT_CODE_GRAPH_ACCESS_LIST) travels to the children via env
#   inheritance, exactly as before the merge.
#
# MUST MATCH: post-tool-use-async.ps1 — the ROUTE_TABLE below is mirrored
# byte-for-byte in the sibling (shared-config tier of the cross-language
# rule); tests/test_v02101_async_posttooluse_dispatcher.py parses BOTH and
# pins them equal, and pins the stem set against the v0.2.101 retirement
# rows so a sub-hook can never be routed on one OS only.
#
# Routing table format: <tool_name or *>|<script stem>|<gate>
#   gate `-`                 = always route on a tool match
#   gate `git-commit-prefix` = only when tool_input.command starts with
#                              "git commit" (the retired `if` key)

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# ROUTING TABLE — the ONE declaration (parsed by the loop below, mirrored
# in the .ps1 sibling, read by the tests). Do not reformat.
ROUTE_TABLE='
Edit|post-edit-outcome|-
Edit|kg-summary-generator|-
Write|post-edit-outcome|-
Write|kg-summary-generator|-
Bash|post-bash-context-record|-
Bash|post-git-commit-kg-sync|git-commit-prefix
Bash|post-file-delete|-
mcp__weaviate-kg__store_knowledge_node|kg-summary-generator|-
*|kg-update-nudge|-
'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the failure log BEFORE going silent (vco_metrics_dir mkdir -p's
# its target; an empty result logs to /dev/null and never fails the hook —
# conservative default on a best-effort path).
ASYNC_LOG="/dev/null"
_PTA_LIB="$SCRIPT_DIR/_lib/metrics-dir.sh"
if [ -f "$_PTA_LIB" ]; then
    # shellcheck source=_lib/metrics-dir.sh disable=SC1091
    . "$_PTA_LIB"
    _PTA_DIR="$(vco_metrics_dir 2>/dev/null || printf '')"
    if [ -n "$_PTA_DIR" ]; then
        ASYNC_LOG="$_PTA_DIR/post-tool-use-async.log"
    fi
fi

# SILENCE GUARANTEE: from here nothing this process — or anything it
# spawns without an explicit redirect — writes on stdout/stderr can reach
# the harness. This is the structural fix: an async PostToolUse hook's
# output becomes a transcript attachment, so the dispatcher has no output.
exec >/dev/null 2>>"$ASYNC_LOG"

# PER-SUB-HOOK DISABLE (v0.2.101 review SF-2): a stem listed in
# VCO_ASYNC_DISABLED_HOOKS (<project>/.claude/env — the SAME per-project
# hook-knob channel lean-ctx's VCO_LEAN_CTX_DEFAULT uses; comma-separated
# stems) is skipped, so the merge kept the per-registration toggle the
# eight retired async entries had. The launcher's Hooks tab writes the key
# (set_claude_env_value, the lean-ctx toggle's own command) and a bundle
# update carries a pre-merge parked disable into it
# (vco_lib.hook_retirements.carry_parked_async_disables). Read exactly the
# way lean-ctx-rewrite.sh reads its knob: source the anchored file (last
# assignment wins; the file outranks a pre-set process env, same
# semantics), after the silence exec so a noisy env file cannot reach the
# harness either.
_PTA_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
# shellcheck source=/dev/null disable=SC1091
# `</dev/null` (re-review N-A): the file is user-editable and a
# stdin-consuming line in it (`read …`) would otherwise EAT the hook
# payload before `cat >"$TMP_INPUT"` below, silently degrading routing to
# the `*` row. Sourcing the knob must never touch the dispatcher's stdin.
[ -f "$_PTA_ROOT/.claude/env" ] && . "$_PTA_ROOT/.claude/env" </dev/null
# tr -d '\r ': a CRLF-written .claude/env leaves a trailing \r on a sourced
# value, and a hand-edited list may carry spaces around the commas — either
# would defeat the exact-stem match below. Hook stems never contain spaces,
# so dropping every space + CR is the .sh half of the .ps1 sibling's
# per-stem Trim() (same accepted spellings on both OSes).
_PTA_DISABLED=",$(printf '%s' "${VCO_ASYNC_DISABLED_HOOKS:-}" | tr -d '\r '),"

# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python — cannot route; sub-hooks need it too

# Read stdin ONCE to a temp file every sub-hook shares (mktemp creates it
# 0600). TMPDIR is honoured by mktemp itself — no hardcoded path.
TMP_INPUT="$(mktemp 2>/dev/null)" || exit 0
WORKDIR="$(mktemp -d 2>/dev/null)" || { rm -f "$TMP_INPUT"; exit 0; }
trap 'rm -f "$TMP_INPUT"; rm -rf "$WORKDIR"' EXIT
# v0.2.101 review N-3: also clean up when the harness TERMINATES us (the
# 15 s timeout path sends a signal before any kill): exit 0 on TERM/INT/HUP
# runs the EXIT trap (children, already backgrounded, are not signalled by
# this — same as the pre-merge harness timeout, which killed only the one
# hook process). SIGKILL cannot be trapped — a hard kill still leaks the
# 0600 payload file into TMPDIR; that limit is documented, not solvable
# from inside the process (the .ps1 sibling's `finally` has the same bound).
trap 'exit 0' TERM INT HUP
cat >"$TMP_INPUT" || exit 0
[ -s "$TMP_INPUT" ] || exit 0

# Routing fields, one Python call: line 1 = tool_name, line 2 = "1" when
# tool_input.command starts with "git commit" (the retired registration
# gate), else "0". A malformed payload yields an empty tool_name: only the
# `*` row fires then, whose script soft-fails on the payload internally —
# the same net effect the retired `*` registration had.
_ROUTE_FIELDS=$("$PY" -c '
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as f:
        d = json.load(f)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
ti = d.get("tool_input")
cmd = (ti.get("command") or "") if isinstance(ti, dict) else ""
c = cmd.lstrip()
print(d.get("tool_name") or "")
# Word-boundary match for the retired `if: Bash(git commit *)` key (review
# N-1): `git commit` exactly or followed by a space — NOT `git commit-tree`.
print("1" if (c == "git commit" or c.startswith("git commit ")) else "0")
' "$TMP_INPUT" 2>/dev/null) || _ROUTE_FIELDS=""
TOOL_NAME=$(printf '%s' "$_ROUTE_FIELDS" | sed -n '1p')
IS_GIT_COMMIT=$(printf '%s' "$_ROUTE_FIELDS" | sed -n '2p')

# Concurrent fan-out + wait: the harness used to run the separate async
# registrations concurrently, and `wait` keeps this hook inside its single
# registration timeout instead of summing the per-hook budgets.
_PTA_PIDS=()
_PTA_NAMES=()
while IFS='|' read -r _pta_tool _pta_script _pta_gate; do
    [ -n "$_pta_tool" ] || continue
    if [ "$_pta_tool" != "*" ] && [ "$_pta_tool" != "$TOOL_NAME" ]; then
        continue
    fi
    if [ "$_pta_gate" = "git-commit-prefix" ] && [ "$IS_GIT_COMMIT" != "1" ]; then
        continue
    fi
    case "$_PTA_DISABLED" in
        *",$_pta_script,"*) continue ;;  # per-sub-hook disable (SF-2)
    esac
    _pta_path="$SCRIPT_DIR/$_pta_script.sh"
    if [ ! -f "$_pta_path" ]; then
        printf '%s post-tool-use-async %s missing\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_pta_script" >>"$ASYNC_LOG"
        continue
    fi
    bash "$_pta_path" <"$TMP_INPUT" >/dev/null 2>"$WORKDIR/$_pta_script.err" &
    _PTA_PIDS+=("$!")
    _PTA_NAMES+=("$_pta_script")
done <<ROUTE_EOF
$ROUTE_TABLE
ROUTE_EOF

_pta_i=0
while [ "$_pta_i" -lt "${#_PTA_PIDS[@]}" ]; do
    _pta_rc=0
    wait "${_PTA_PIDS[$_pta_i]}" || _pta_rc=$?
    _pta_script="${_PTA_NAMES[$_pta_i]}"
    _pta_err="$WORKDIR/$_pta_script.err"
    if [ "$_pta_rc" -ne 0 ] || [ -s "$_pta_err" ]; then
        _pta_tail=$(tr '\n\r' '  ' <"$_pta_err" 2>/dev/null | cut -c1-500)
        printf '%s post-tool-use-async %s exit=%s stderr=%s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_pta_script" "$_pta_rc" \
            "$_pta_tail" >>"$ASYNC_LOG"
    fi
    rm -f "$_pta_err"
    _pta_i=$((_pta_i + 1))
done

exit 0
