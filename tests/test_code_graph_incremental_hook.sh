#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# v0.2.47 (extras): integration tests for the code-graph-incremental.sh
# hook's new extras-path detection. The hook normally shells out to the
# analyzer in the background; we override that via VCT_PYTHON +
# VCT_ANALYZER_SCRIPT to point at a Python stub that just records its
# argv. The assertion compares the recorded (REPO_PATH, --project value)
# pair against the expected match-target for each scenario.
#
# Scenarios exercised:
#   1. Edit under current $REPO_PATH                 → existing behaviour
#                                                     (REPO_PATH unchanged)
#   2. Edit under an enabled extra path              → REPO_PATH=extra,
#                                                     project unchanged
#   3. Edit under a disabled extra path              → falls through to
#                                                     sibling detection,
#                                                     then no-match exit 0
#   4. Edit under a sibling-but-not-extra            → sibling detection
#                                                     re-points REPO_PATH
#                                                     to the sibling AND
#                                                     project name
#   5. Edit under no known root (no extras, no sib)  → exit 0 silently
#   6. Pre-v0.2.47 hub (no extras field at all)      → existing behaviour
#                                                     (sibling detection
#                                                     runs, no-match exit)
#
# Hub is faked via a tiny python http.server; the resolver picks it up
# through $VCT_HUB_PORT + $VCT_HUB_TOKEN.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/.claude/hooks/code-graph-incremental.sh"
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
        if p.query: k += "_" + p.query.replace("/", "_")
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

scratch="$(mktemp -d)"
trap "stop_fake_hub '$scratch/hub.pid' || true; rm -rf '$scratch'" EXIT
mkdir -p "$scratch/responses"

# Project root: pretend this is an installed-into-user-project tree.
PROJECT_ROOT="$scratch/projects/MyProject"
mkdir -p "$PROJECT_ROOT/.claude/scripts" "$PROJECT_ROOT/src"

# Drop in the project's resolver + the hook + a python-resolver stub
# that the hook can find. The hook resolves the analyzer via
# VCT_ANALYZER_SCRIPT / VCT_PYTHON env vars, so we pin those below.
cp "$REPO_ROOT/.claude/scripts/vct_project_config.sh" \
   "$PROJECT_ROOT/.claude/scripts/vct_project_config.sh"
chmod +x "$PROJECT_ROOT/.claude/scripts/vct_project_config.sh"
# Sibling-detection helper — required by the hook's existing path.
mkdir -p "$PROJECT_ROOT/.claude/scripts"
cp "$REPO_ROOT/.claude/scripts/detect-project.sh" \
   "$PROJECT_ROOT/.claude/scripts/detect-project.sh"
chmod +x "$PROJECT_ROOT/.claude/scripts/detect-project.sh"

# Sibling project (used by scenario 4).
SIBLING_ROOT="$scratch/projects/SiblingProject"
mkdir -p "$SIBLING_ROOT/src"

# Extra path target (used by scenario 2 + 3).
EXTRA_PATH="$scratch/extras/vibecoded-orchestrator"
mkdir -p "$EXTRA_PATH/launcher/src-tauri/src"

# Analyzer stub: records (cwd, --project value, REPO_PATH arg) to a JSONL
# file. The first positional arg is the repo_path the hook hands us.
ANALYZER_STUB="$scratch/analyzer_stub.py"
ARGV_LOG="$scratch/argv.jsonl"
cat > "$ANALYZER_STUB" <<PYEOF
#!/usr/bin/env python3
import json, os, sys
row = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
}
with open(os.environ["VCT_ARGV_LOG"], "a") as f:
    f.write(json.dumps(row) + "\n")
PYEOF
chmod +x "$ANALYZER_STUB"

# Pin the hub for the resolver.
export VCT_STATE_DIR="$scratch/state-dir"
mkdir -p "$VCT_STATE_DIR"
export VCT_HUB_TOKEN="canary-hook-test-v0247"
start_fake_hub "$scratch/responses" "$scratch/hub.port" "$scratch/hub.pid"
HUB_PORT=$(cat "$scratch/hub.port")
export VCT_HUB_PORT="$HUB_PORT"

PROJECT_ID="abc-12345-extras"

# Resolver fixtures — two paths the hook calls:
#   1) `--field code_graph_collection_prefix` to pick up PROJECT_NAME
#   2) `--field code_graph_extra_paths` to pick up extras
# We provide BOTH so the hook's existing PROJECT_NAME flow is also
# exercised, not just the new extras flow.
write_fixture() {
    local key="$1" body="$2" status="${3:-200}"
    local path="$scratch/responses/GET_projects_${PROJECT_ID}_config_key=${key}.json"
    printf '%s' "$body" > "$path"
    if [[ "$status" != "200" ]]; then
        printf '%s' "$status" > "${path}.status"
    fi
}

# by-path lookup so the resolver maps the project root → PROJECT_ID.
# The bash resolver's url_encode keeps `/` unescaped (safe set
# `[a-zA-Z0-9._~/-]`); the fake hub then converts `/` → `_` over the
# entire path+query key string. Mirror that here so the fixture
# filename matches what the fake hub looks up at request time.
qkey_filename=$(printf '%s' "projects_by-path_path=${PROJECT_ROOT}" | tr '/' '_')
cat > "$scratch/responses/GET_${qkey_filename}.json" <<EOF
{"id":"$PROJECT_ID"}
EOF

write_fixture "code_graph_collection_prefix" '{"code_graph_collection_prefix":"MyProject"}'

# Helper: clear the argv log between scenarios.
reset_argv_log() { : > "$ARGV_LOG"; }
read_argv_log() { cat "$ARGV_LOG" 2>/dev/null || true; }

run_hook() {
    # The hook spawns the analyzer in the background (`( ... ) &`). We
    # need to wait for the background job before reading the log, so we
    # also set VCT_PYTHON to a wrapper that's synchronous and the
    # outer hook's `&` only delays a few ms. We poll for the log line
    # afterward instead of relying on `wait`.
    local edited_file="$1"
    set +e
    VCT_ANALYZER_SCRIPT="$ANALYZER_STUB" \
    VCT_PYTHON="$(command -v python3)" \
    VCT_ARGV_LOG="$ARGV_LOG" \
        bash "$HOOK" "$edited_file" "$PROJECT_ROOT" 2>/dev/null
    local rc=$?
    set -e
    # Wait up to 2s for the background analyzer to write its row.
    for _ in $(seq 1 40); do
        if [[ -s "$ARGV_LOG" ]]; then break; fi
        sleep 0.05
    done
    return $rc
}

# ── Scenario 1: edit under current $REPO_PATH (own repo) ───────────────
reset_argv_log
# Extras list is non-empty but the edited file is under the project's own
# repo — the early-return case (1) skips the extras query entirely.
write_fixture "code_graph_extra_paths" \
    "{\"code_graph_extra_paths\":[{\"path\":\"$EXTRA_PATH\",\"enabled\":true,\"last_indexed_commit\":null}]}"
echo "# stub" > "$PROJECT_ROOT/src/main.py"
run_hook "$PROJECT_ROOT/src/main.py" || true
log=$(read_argv_log)
# argv[0] = repo_path. For own-repo edits, repo_path stays = PROJECT_ROOT.
assert_contains "$log" "\"$PROJECT_ROOT\"" \
    "own-repo edit → analyzer cwd is the primary repo"
assert_contains "$log" '"--project", "MyProject"' \
    "own-repo edit → analyzer --project is MyProject"
# Extras query should NOT have been wired in (the case branch short-
# circuited). We don't have a direct way to assert "no hub call to
# extras" from inside the hook, but we DO know the REPO_PATH stayed at
# PROJECT_ROOT and the analyzer got that path.
assert_contains "$log" "\"$PROJECT_ROOT\"" "REPO_PATH unchanged for own-repo edit"

# ── Scenario 2: edit under an enabled extra path ───────────────────────
reset_argv_log
write_fixture "code_graph_extra_paths" \
    "{\"code_graph_extra_paths\":[{\"path\":\"$EXTRA_PATH\",\"enabled\":true,\"last_indexed_commit\":null}]}"
echo "// stub" > "$EXTRA_PATH/launcher/src-tauri/src/main.rs"
run_hook "$EXTRA_PATH/launcher/src-tauri/src/main.rs" || true
log=$(read_argv_log)
# REPO_PATH should now be the extra path, project still MyProject.
assert_contains "$log" "\"$EXTRA_PATH\"" \
    "edit under enabled extra → REPO_PATH re-pointed to extra"
assert_contains "$log" '"--project", "MyProject"' \
    "edit under enabled extra → --project stays MyProject (extras index into current project)"

# ── Scenario 3: edit under a DISABLED extra path ───────────────────────
# Disabled rows are filtered out by the resolver. Without an extras
# match, sibling detection runs; the file ISN'T under any sibling either
# (it's under $scratch/extras, not $scratch/projects), so the hook
# returns silently — analyzer not invoked.
reset_argv_log
write_fixture "code_graph_extra_paths" \
    "{\"code_graph_extra_paths\":[{\"path\":\"$EXTRA_PATH\",\"enabled\":false,\"last_indexed_commit\":null}]}"
# Same file as scenario 2 — different fixture state.
run_hook "$EXTRA_PATH/launcher/src-tauri/src/main.rs" || true
log=$(read_argv_log)
# Background analyzer may have been spawned but with REPO_PATH still =
# PROJECT_ROOT (file isn't actually under it, but sibling detection's
# dirname approach picks up parent_dir = $scratch/projects; the file's
# under $scratch/extras so detect_project_for_file returns ""). The
# analyzer still runs against PROJECT_ROOT.
# We accept either "log is empty" (the hook bailed early) OR "log shows
# PROJECT_ROOT" (analyzer ran against the project's own repo because no
# match was found). Both are graceful — the key invariant is that
# REPO_PATH does NOT become the disabled extra path.
if [[ -z "$log" ]]; then
    printf '  PASS  disabled extra → hook bailed early (no analyzer invocation)\n'
    PASS=$((PASS + 1))
else
    if [[ "$log" == *"\"$EXTRA_PATH\""* ]]; then
        printf '  FAIL  disabled extra was incorrectly used as REPO_PATH\n' >&2
        printf '         log: %q\n' "$log" >&2
        FAIL=$((FAIL + 1))
    else
        printf '  PASS  disabled extra → REPO_PATH not re-pointed to it\n'
        PASS=$((PASS + 1))
    fi
fi

# ── Scenario 4: sibling-but-not-extra → existing sibling detection ─────
reset_argv_log
# Empty extras list — sibling detection should fire.
write_fixture "code_graph_extra_paths" '{"code_graph_extra_paths":[]}'
echo "# stub" > "$SIBLING_ROOT/src/lib.py"
run_hook "$SIBLING_ROOT/src/lib.py" || true
log=$(read_argv_log)
# detect-project.sh: SIBLING_ROOT is a sibling of PROJECT_ROOT under
# $scratch/projects/. The helper detects it and re-points REPO_PATH +
# PROJECT_NAME to the sibling.
assert_contains "$log" "\"$SIBLING_ROOT\"" \
    "sibling-but-not-extra → REPO_PATH re-pointed to sibling"
assert_contains "$log" '"--project", "SiblingProject"' \
    "sibling-but-not-extra → --project becomes sibling name"

# ── Scenario 5: edit under unknown root (no match anywhere) ────────────
reset_argv_log
write_fixture "code_graph_extra_paths" '{"code_graph_extra_paths":[]}'
unknown_root="$scratch/unknown/somewhere/else"
mkdir -p "$unknown_root"
echo "# stub" > "$unknown_root/foo.py"
run_hook "$unknown_root/foo.py" || true
log=$(read_argv_log)
# The hook STILL invokes the analyzer with PROJECT_ROOT as cwd (the
# pre-v0.2.47 fallback). The analyzer would either find no files OR
# analyze PROJECT_ROOT's own contents — neither is harmful. The
# invariant we care about is "REPO_PATH was NOT silently re-pointed to
# something under $scratch/unknown/".
if [[ "$log" == *"\"$scratch/unknown"* ]]; then
    printf '  FAIL  unknown-root edit was treated as a match\n' >&2
    printf '         log: %q\n' "$log" >&2
    FAIL=$((FAIL + 1))
else
    printf '  PASS  unknown-root edit → REPO_PATH unchanged (falls through gracefully)\n'
    PASS=$((PASS + 1))
fi

# ── Scenario 6: pre-v0.2.47 hub (no code_graph_extra_paths field) ──────
reset_argv_log
# Return 404 field_not_found — the bash resolver maps this to exit 4
# and the hook's EXTRAS_LIST stays empty. Sibling detection then runs
# normally. Use the sibling file so we see the sibling-rewrite happen.
fpath="$scratch/responses/GET_projects_${PROJECT_ID}_config_key=code_graph_extra_paths.json"
cat > "$fpath" <<'JSON'
{"error":{"code":"field_not_found","message":"unknown field"}}
JSON
echo "404" > "$fpath.status"
run_hook "$SIBLING_ROOT/src/lib.py" || true
log=$(read_argv_log)
# Pre-v0.2.47 hub: sibling detection still fires for the sibling file.
assert_contains "$log" "\"$SIBLING_ROOT\"" \
    "pre-v0.2.47 hub → sibling detection still fires (back-compat)"
assert_contains "$log" '"--project", "SiblingProject"' \
    "pre-v0.2.47 hub → --project becomes sibling name (back-compat)"

# ── Summary ────────────────────────────────────────────────────────────
echo
echo "─────────────────────────────────────────────"
if (( FAIL == 0 )); then
    echo "OK: $PASS tests passed"
    exit 0
else
    echo "FAIL: $PASS passed, $FAIL failed"
    exit 1
fi
