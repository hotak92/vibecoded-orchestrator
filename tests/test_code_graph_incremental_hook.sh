#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Integration tests for the STANDALONE code-graph-incremental.sh hook
# (templates/hooks/code-graph-incremental.sh). The hook ships unregistered —
# its scheduling moved to stop-codegraph-drain.sh in v0.2.73 — and is kept for
# users who wire it themselves, so it must still pick the right analyzer
# arguments on its own. Run by tests/test_shell_suites_runner.py under pytest.
#
# The hook is installed into a fixture project exactly as a bundle install
# lays it out (<project>/.claude/hooks/ + _lib/, <project>/.claude/scripts/),
# and the fixture repos are real git repositories: since v0.2.66 the hook
# resolves the edited file's canonical git root and SKIPS a non-git path under
# the temp dir as transient scratch, so a non-git /tmp fixture never reaches
# the analyzer at all (that is how the pre-v0.2.102 version of this file
# failed 9 of 11 asserts without anyone noticing — nothing ran it).
#
# The analyzer is replaced via VCT_ANALYZER_SCRIPT + VCT_PYTHON by a stub that
# records its argv to a JSONL file; nothing here touches Weaviate. The hub is a
# tiny python http.server fake reached through $VCT_HUB_PORT.
#
# Decisions asserted (each against the parsed argv, not a substring):
#   1. own-repo edit          → repo_path = project, --project = hub prefix,
#                                --canonical-source = project, no --as-extra-path
#   2. enabled extra path     → repo_path = extra, --project = the CURRENT
#                                project's prefix, --as-extra-path (the same
#                                ownership guard every other extra-path
#                                analyzer run passes)
#   3. disabled extra path    → never re-pointed to the extra
#   4. sibling project        → repo_path + --project = the sibling
#   5. non-git scratch path   → analyzer not invoked (v0.2.66 scratch skip)
#   6. pre-v0.2.47 hub        → sibling detection still fires
#   7. hub unreachable        → --project falls back to the root's basename
#   8. non-code file          → analyzer not invoked

#
# Usage: test_code_graph_incremental_hook.sh [sh|ps1]
#   sh  (default) the bash hook with the bash resolver + detect-project.sh
#   ps1 the PowerShell sibling under pwsh, with the .ps1 resolver and
#       detect-project.ps1 — the SAME scenarios, so the pair stays in parity.

set -euo pipefail

FLAVOR="${1:-sh}"
case "$FLAVOR" in
    sh|ps1) ;;
    *) echo "usage: $0 [sh|ps1]" >&2; exit 2 ;;
esac
if [[ "$FLAVOR" == "ps1" ]] && ! command -v pwsh >/dev/null 2>&1; then
    echo "SKIP: pwsh not on PATH (ps1 flavor)"
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_HOOK="$REPO_ROOT/templates/hooks/code-graph-incremental.$FLAVOR"
SRC_HOOK_LIB="$REPO_ROOT/templates/hooks/_lib"
SRC_SCRIPTS="$REPO_ROOT/templates/scripts"
PASS=0
FAIL=0

# ── Mini HTTP fake (same shape as the secrets-resolve / config tests) ──
start_fake_hub() {
    local responses_dir="$1"
    local port_file="$2"
    local pid_file="$3"

    python3 - <<'PYEOF' >"$port_file"
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PYEOF
    local port
    port=$(cat "$port_file")

    python3 - "$responses_dir" "$port" <<'PYEOF' &
import http.server, json, os, sys, urllib.parse
responses_dir = sys.argv[1]; port = int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a,**k): pass
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path.removeprefix("/api/v1/").rstrip("/")
        k = path.replace("/", "_")
        # Decode first: the .ps1 resolver percent-encodes the by-path query
        # (`/` -> %2F) where the bash one leaves `/` literal; both must map
        # to the same fixture file.
        if p.query: k += "_" + urllib.parse.unquote(p.query).replace("/", "_")
        bp = os.path.join(responses_dir, f"GET_{k}.json")
        sp = bp + ".status"
        if not os.path.exists(bp):
            self.send_response(404); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps({"error":{"code":"no_fixture","message":bp}}).encode()); return
        st = 200
        if os.path.exists(sp):
            st = int(open(sp).read().strip())
        body = open(bp).read().encode()
        self.send_response(st); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PYEOF
    echo $! >"$pid_file"
    for _ in $(seq 1 30); do
        curl -s --max-time 0.5 "http://127.0.0.1:${port}/api/v1/__wakeup__" >/dev/null 2>&1 && break
        sleep 0.05
    done
}

stop_fake_hub() {
    local pid_file="$1"
    [[ -f "$pid_file" ]] && kill "$(cat "$pid_file")" 2>/dev/null || true
    rm -f "$pid_file"
}

assert_eq() {
    local got="$1" want="$2" desc="$3"
    if [[ "$got" == "$want" ]]; then
        printf '  PASS  %s\n' "$desc"; PASS=$((PASS + 1))
    else
        printf '  FAIL  %s\n' "$desc" >&2
        printf '         got:  %q\n' "$got" >&2
        printf '         want: %q\n' "$want" >&2
        FAIL=$((FAIL + 1))
    fi
}

assert_ne() {
    local got="$1" unwanted="$2" desc="$3"
    if [[ "$got" != "$unwanted" ]]; then
        printf '  PASS  %s\n' "$desc"; PASS=$((PASS + 1))
    else
        printf '  FAIL  %s\n' "$desc" >&2
        printf '         got the unwanted value: %q\n' "$got" >&2
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local got="$1" needle="$2" desc="$3"
    if [[ "$got" == *"$needle"* ]]; then
        printf '  PASS  %s\n' "$desc"; PASS=$((PASS + 1))
    else
        printf '  FAIL  %s\n' "$desc" >&2
        printf '         got does not contain: %q\n' "$needle" >&2
        printf '         actual: %q\n' "$got" >&2
        FAIL=$((FAIL + 1))
    fi
}

# Physical path: the hook compares against `pwd -P`-normalized git roots.
scratch="$(cd "$(mktemp -d)" && pwd -P)"
trap 'stop_fake_hub "$scratch/hub.pid" || true; rm -rf "$scratch"' EXIT
mkdir -p "$scratch/responses"

# A git repo with no commits is enough: the hook only asks
# `git rev-parse --git-common-dir` for the canonical root.
git_init() { git init -q "$1" >/dev/null 2>&1; }

# ── Fixture project: the bundle-install layout ─────────────────────────
PROJECT_ROOT="$scratch/projects/MyProject"
mkdir -p "$PROJECT_ROOT/.claude/hooks" "$PROJECT_ROOT/.claude/scripts" "$PROJECT_ROOT/src"
git_init "$PROJECT_ROOT"
cp "$SRC_HOOK" "$PROJECT_ROOT/.claude/hooks/code-graph-incremental.$FLAVOR"
cp -R "$SRC_HOOK_LIB" "$PROJECT_ROOT/.claude/hooks/_lib"
for s in "vct_project_config.$FLAVOR" "detect-project.$FLAVOR"; do
    cp "$SRC_SCRIPTS/$s" "$PROJECT_ROOT/.claude/scripts/$s"
    chmod +x "$PROJECT_ROOT/.claude/scripts/$s"
done
HOOK="$PROJECT_ROOT/.claude/hooks/code-graph-incremental.$FLAVOR"

# Sibling project (scenarios 4 + 6): a separate git repo next to the project.
SIBLING_ROOT="$scratch/projects/SiblingProject"
mkdir -p "$SIBLING_ROOT/src"
git_init "$SIBLING_ROOT"

# Extra path target (scenarios 2 + 3): a separate clone outside projects/.
EXTRA_PATH="$scratch/extras/vibecoded-orchestrator"
mkdir -p "$EXTRA_PATH/launcher/src-tauri/src"
git_init "$EXTRA_PATH"

# Analyzer stub: records argv (argv[0] = the repo_path the hook hands over).
ANALYZER_STUB="$scratch/analyzer_stub.py"
ARGV_LOG="$scratch/argv.jsonl"
cat > "$ANALYZER_STUB" <<'PYEOF'
import json, os, sys
with open(os.environ["VCT_ARGV_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
PYEOF

export VCT_STATE_DIR="$scratch/state-dir"
mkdir -p "$VCT_STATE_DIR"
export VCT_HUB_TOKEN="canary-hook-test-v0247"
start_fake_hub "$scratch/responses" "$scratch/hub.port" "$scratch/hub.pid"
HUB_PORT=$(cat "$scratch/hub.port")
export VCT_HUB_PORT="$HUB_PORT"

PROJECT_ID="abc-12345-extras"
# Deliberately NOT the folder basename, so a hub-resolved prefix and the
# basename fallback (scenario 7) are distinguishable.
HUB_PREFIX="MyProjectPrefix"

write_fixture() {
    local key="$1" body="$2" status="${3:-200}"
    local path="$scratch/responses/GET_projects_${PROJECT_ID}_config_key=${key}.json"
    printf '%s' "$body" > "$path"
    rm -f "${path}.status"
    if [[ "$status" != "200" ]]; then
        printf '%s' "$status" > "${path}.status"
    fi
}

# by-path lookup so the resolver maps the project root → PROJECT_ID. The bash
# resolver's url_encode keeps `/` unescaped; the fake hub converts `/` → `_`
# over the whole path+query key, so mirror that for the fixture filename.
qkey_filename=$(printf '%s' "projects_by-path_path=${PROJECT_ROOT}" | tr '/' '_')
cat > "$scratch/responses/GET_${qkey_filename}.json" <<EOF
{"id":"$PROJECT_ID"}
EOF

write_fixture "code_graph_collection_prefix" "{\"code_graph_collection_prefix\":\"$HUB_PREFIX\"}"

# ── Running the hook + reading what the analyzer was handed ────────────
HOOK_OUT="$scratch/hook.stdout"
HOOK_ERR="$scratch/hook.stderr"

run_hook() {
    local edited_file="$1"
    : > "$ARGV_LOG"
    local runner=(bash "$HOOK")
    [[ "$FLAVOR" == "ps1" ]] && runner=(pwsh -NoProfile -NonInteractive -File "$HOOK")
    set +e
    VCT_ANALYZER_SCRIPT="$ANALYZER_STUB" \
    VCT_PYTHON="$(command -v python3)" \
    VCT_ARGV_LOG="$ARGV_LOG" \
        "${runner[@]}" "$edited_file" "$PROJECT_ROOT" >"$HOOK_OUT" 2>"$HOOK_ERR"
    set -e
    if [[ -n "${CGI_TEST_DEBUG:-}" ]]; then
        printf '  [debug] %s\n    stdout: %s\n    stderr: %s\n' "$edited_file" \
            "$(cat "$HOOK_OUT")" "$(cat "$HOOK_ERR")" >&2
    fi
    # The analyzer runs in a background subshell. Only wait for its row when
    # the hook says it queued one; a hook that bailed early spawned nothing.
    if grep -q "Code graph incremental update queued" "$HOOK_OUT"; then
        for _ in $(seq 1 100); do
            [[ -s "$ARGV_LOG" ]] && break
            sleep 0.05
        done
    fi
}

# argv_get <field>: read the single recorded analyzer row.
#   invocations  number of analyzer runs recorded ("0" when none)
#   repo         positional repo_path
#   project      value after --project
#   canon        value after --canonical-source
#   only         value after --only-file
#   extra_flag   "yes" when --as-extra-path was passed, else "no"
#   dot_claude   the --index-dot-claude / --no-index-dot-claude flag
argv_get() {
    python3 - "$ARGV_LOG" "$1" <<'PYEOF'
import json, sys
log, field = sys.argv[1], sys.argv[2]
try:
    rows = [json.loads(l) for l in open(log) if l.strip()]
except FileNotFoundError:
    rows = []
if field == "invocations":
    print(len(rows)); sys.exit(0)
if not rows:
    print("<no-invocation>"); sys.exit(0)
argv = rows[-1]["argv"]
def after(flag):
    return argv[argv.index(flag) + 1] if flag in argv else "<absent>"
out = {
    "repo": argv[0] if argv else "<absent>",
    "project": after("--project"),
    "canon": after("--canonical-source"),
    "only": after("--only-file"),
    "extra_flag": "yes" if "--as-extra-path" in argv else "no",
    "dot_claude": next((a for a in argv if a in ("--index-dot-claude", "--no-index-dot-claude")), "<absent>"),
}[field]
print(out)
PYEOF
}

# ── Scenario 1: edit under the project's own repo ──────────────────────
write_fixture "code_graph_extra_paths" \
    "{\"code_graph_extra_paths\":[{\"path\":\"$EXTRA_PATH\",\"enabled\":true,\"last_indexed_commit\":null}]}"
echo "# stub" > "$PROJECT_ROOT/src/main.py"
run_hook "$PROJECT_ROOT/src/main.py"
assert_eq "$(argv_get invocations)" "1" "own-repo edit → analyzer invoked once"
assert_eq "$(argv_get repo)" "$PROJECT_ROOT" "own-repo edit → repo_path is the project root"
assert_eq "$(argv_get project)" "$HUB_PREFIX" "own-repo edit → --project is the hub's collection prefix"
assert_eq "$(argv_get only)" "$PROJECT_ROOT/src/main.py" "own-repo edit → --only-file is the edited file"
assert_eq "$(argv_get canon)" "$PROJECT_ROOT" "own-repo edit → --canonical-source is the project's git root"
assert_eq "$(argv_get extra_flag)" "no" "own-repo edit → no --as-extra-path"
assert_eq "$(argv_get dot_claude)" "--no-index-dot-claude" \
    "own-repo edit → user project excludes .claude/ (hub field absent, not a VCO clone)"

# ── Scenario 2: edit under an ENABLED extra path ───────────────────────
echo "// stub" > "$EXTRA_PATH/launcher/src-tauri/src/main.rs"
run_hook "$EXTRA_PATH/launcher/src-tauri/src/main.rs"
assert_eq "$(argv_get invocations)" "1" "enabled extra → analyzer invoked once"
assert_eq "$(argv_get repo)" "$EXTRA_PATH" "enabled extra → repo_path re-pointed to the extra"
assert_eq "$(argv_get project)" "$HUB_PREFIX" \
    "enabled extra → --project stays the current project's prefix (extras index into it)"
assert_eq "$(argv_get canon)" "$EXTRA_PATH" "enabled extra → --canonical-source is the extra's git root"
assert_eq "$(argv_get extra_flag)" "yes" \
    "enabled extra → --as-extra-path (deletes limited to rows provably the extra's)"

# ── Scenario 3: edit under a DISABLED extra path ───────────────────────
# Disabled rows are filtered by the resolver; the file is under no sibling
# either. Whatever the hook does next, the extra must not become repo_path
# and the run must not be flagged as an extra-path run.
write_fixture "code_graph_extra_paths" \
    "{\"code_graph_extra_paths\":[{\"path\":\"$EXTRA_PATH\",\"enabled\":false,\"last_indexed_commit\":null}]}"
run_hook "$EXTRA_PATH/launcher/src-tauri/src/main.rs"
assert_ne "$(argv_get repo)" "$EXTRA_PATH" "disabled extra → repo_path never re-pointed to it"
assert_ne "$(argv_get extra_flag)" "yes" "disabled extra → never analyzed as an extra path"

# ── Scenario 4: sibling project (no extras) ────────────────────────────
write_fixture "code_graph_extra_paths" '{"code_graph_extra_paths":[]}'
echo "# stub" > "$SIBLING_ROOT/src/lib.py"
run_hook "$SIBLING_ROOT/src/lib.py"
assert_eq "$(argv_get invocations)" "1" "sibling edit → analyzer invoked once"
assert_eq "$(argv_get repo)" "$SIBLING_ROOT" "sibling edit → repo_path re-pointed to the sibling"
assert_eq "$(argv_get project)" "SiblingProject" "sibling edit → --project becomes the sibling's name"
assert_eq "$(argv_get canon)" "$SIBLING_ROOT" "sibling edit → --canonical-source is the sibling's git root"
assert_eq "$(argv_get extra_flag)" "no" "sibling edit → no --as-extra-path"

# ── Scenario 5: non-git path under the temp dir → scratch skip ─────────
unknown_root="$scratch/unknown/somewhere/else"
mkdir -p "$unknown_root"
echo "# stub" > "$unknown_root/foo.py"
run_hook "$unknown_root/foo.py"
assert_eq "$(argv_get invocations)" "0" "non-git temp-dir edit → analyzer NOT invoked"
assert_contains "$(cat "$HOOK_ERR")" "transient scratch path" \
    "non-git temp-dir edit → the skip is announced on stderr"

# ── Scenario 6: pre-v0.2.47 hub (no code_graph_extra_paths field) ──────
write_fixture "code_graph_extra_paths" \
    '{"error":{"code":"field_not_found","message":"unknown field"}}' 404
run_hook "$SIBLING_ROOT/src/lib.py"
assert_eq "$(argv_get repo)" "$SIBLING_ROOT" "pre-v0.2.47 hub → sibling detection still fires"
assert_eq "$(argv_get project)" "SiblingProject" "pre-v0.2.47 hub → --project becomes the sibling's name"

# ── Scenario 7: hub unreachable → basename fallback ────────────────────
stop_fake_hub "$scratch/hub.pid"
run_hook "$PROJECT_ROOT/src/main.py"
assert_eq "$(argv_get invocations)" "1" "hub down → own-repo edit still analyzed"
assert_eq "$(argv_get repo)" "$PROJECT_ROOT" "hub down → repo_path is the project root"
assert_eq "$(argv_get project)" "MyProject" "hub down → --project falls back to the root's basename"

# ── Scenario 8: non-code file → no analyzer run ────────────────────────
echo "notes" > "$PROJECT_ROOT/README.md"
run_hook "$PROJECT_ROOT/README.md"
assert_eq "$(argv_get invocations)" "0" "non-code file → analyzer NOT invoked"

# ── Summary ────────────────────────────────────────────────────────────
echo
echo "─────────────────────────────────────────────"
if (( FAIL == 0 )); then
    echo "OK ($FLAVOR): $PASS tests passed"
    exit 0
else
    echo "FAIL ($FLAVOR): $PASS passed, $FAIL failed"
    exit 1
fi
