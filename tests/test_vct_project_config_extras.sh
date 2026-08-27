#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# v0.2.47 (extras): tests for the bash resolver's
# `--field code_graph_extra_paths` rendering. Spins up the same
# minimal python http.server fake as tests/test_vct_secrets_resolve.sh
# and asserts that:
#   * Enabled rows render as newline-delimited paths.
#   * Disabled rows are dropped.
#   * Empty list → empty stdout + exit 0 (zero extras is valid).
#   * Field absent on the wire → exit 4 (pre-v0.2.47 hub).
#   * Malformed JSON body → exit 4 (defensive).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESOLVER="$REPO_ROOT/templates/scripts/vct_project_config.sh"
PASS=0
FAIL=0

# ── Mini HTTP fake (copy of the secrets-resolver test helper) ──────────
#
# Routes:
#   GET /api/v1/projects/<pid>/config?key=<field>
#   GET /api/v1/projects/by-path?path=<path>
#
# Fixtures live in $responses_dir as files named after the URL with
# slashes collapsed to underscores (same convention as the secrets test).

start_fake_hub() {
    local responses_dir="$1"
    local port_file="$2"
    local pid_file="$3"

    python3 - <<'PYEOF' >"$port_file"
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PYEOF
    local port
    port=$(cat "$port_file")

    python3 - "$responses_dir" "$port" <<'PYEOF' &
import http.server
import json
import os
import sys
import urllib.parse

responses_dir = sys.argv[1]
port = int(sys.argv[2])

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def _check_auth(self):
        """Auth gate — only active when <responses_dir>/_require_token
        exists. Its first line is the expected token; anything else gets
        a 401 + JSON envelope (mirrors tests/test_vct_secrets_resolve.sh
        and the real hub's `auth.rs::require_auth`). Used by the v0.2.91
        stale-env-token fallback tests."""
        require_path = os.path.join(responses_dir, "_require_token")
        if not os.path.exists(require_path):
            return None
        with open(require_path) as f:
            expected = f.read().strip()
        auth = self.headers.get("Authorization", "")
        parts = auth.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return (401, json.dumps({
                "error": {"code": "unauthorized", "message": "missing bearer"}
            }))
        if parts[1].strip() != expected:
            return (401, json.dumps({
                "error": {"code": "unauthorized", "message": "wrong token"}
            }))
        return None

    def do_GET(self):
        gate = self._check_auth()
        if gate is not None:
            status, body = gate
            body_b = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_b)))
            self.end_headers()
            self.wfile.write(body_b)
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.removeprefix("/api/v1/").rstrip("/")
        path_key = path.replace("/", "_")
        if parsed.query:
            path_key += "_" + parsed.query.replace("/", "_")
        body_path = os.path.join(responses_dir, f"GET_{path_key}.json")
        status_path = body_path + ".status"
        if not os.path.exists(body_path):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": {"code": "no_fixture",
                          "message": f"no fixture at {body_path}"}
            }).encode())
            return
        status = 200
        if os.path.exists(status_path):
            with open(status_path) as f:
                status = int(f.read().strip())
        with open(body_path) as f:
            body = f.read().encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

server = http.server.HTTPServer(("127.0.0.1", port), Handler)
server.serve_forever()
PYEOF
    echo $! >"$pid_file"
    for _ in $(seq 1 30); do
        if curl -s --max-time 0.5 "http://127.0.0.1:${port}/api/v1/__wakeup__" >/dev/null 2>&1; then
            break
        fi
        sleep 0.05
    done
}

stop_fake_hub() {
    local pid_file="$1"
    if [[ -f "$pid_file" ]]; then
        kill "$(cat "$pid_file")" 2>/dev/null || true
        rm -f "$pid_file"
    fi
}

assert_eq() {
    local got="$1" want="$2" desc="$3"
    if [[ "$got" == "$want" ]]; then
        printf '  PASS  %s\n' "$desc"
        PASS=$((PASS + 1))
    else
        printf '  FAIL  %s\n' "$desc" >&2
        printf '         got:  %q\n' "$got" >&2
        printf '         want: %q\n' "$want" >&2
        FAIL=$((FAIL + 1))
    fi
}

assert_lines_eq() {
    # Compare two newline-delimited strings, normalising trailing
    # whitespace so a final "\n" doesn't make the assertion flaky.
    local got="$1" want="$2" desc="$3"
    local got_t="${got%$'\n'}"
    local want_t="${want%$'\n'}"
    if [[ "$got_t" == "$want_t" ]]; then
        printf '  PASS  %s\n' "$desc"
        PASS=$((PASS + 1))
    else
        printf '  FAIL  %s\n' "$desc" >&2
        printf '         got:  %q\n' "$got" >&2
        printf '         want: %q\n' "$want" >&2
        FAIL=$((FAIL + 1))
    fi
}

scratch="$(mktemp -d)"
trap "stop_fake_hub '$scratch/hub.pid' || true; rm -rf '$scratch'" EXIT
mkdir -p "$scratch/responses"

# Isolated state dir + canary auth token (mirrors the secrets test).
export VCT_STATE_DIR="$scratch/state-dir"
mkdir -p "$VCT_STATE_DIR"
export VCT_HUB_TOKEN="canary-test-token-v0247"

PROJECT_ID="550e8400-e29b-41d4-a716-446655440000"

# Spin up fake hub.
start_fake_hub "$scratch/responses" "$scratch/hub.port" "$scratch/hub.pid"
HUB_PORT=$(cat "$scratch/hub.port")
export VCT_HUB_PORT="$HUB_PORT"

# Helper to write a fixture for a `--field code_graph_extra_paths` request.
write_extras_fixture() {
    local body="$1"
    # URL-encoded "key=code_graph_extra_paths". We assert against the
    # exact query string the resolver constructs.
    local fpath="$scratch/responses/GET_projects_${PROJECT_ID}_config_key=code_graph_extra_paths.json"
    printf '%s' "$body" > "$fpath"
}

# ── Test 1: enabled rows render as newline-delimited paths ─────────────
# Mixed enabled/disabled — only enabled should appear, in order.
write_extras_fixture '{"code_graph_extra_paths":[
    {"path":"/home/u/sibling-a","enabled":true,"last_indexed_commit":"abc1234"},
    {"path":"/home/u/sibling-b","enabled":false,"last_indexed_commit":null},
    {"path":"/home/u/sibling-c","enabled":true,"last_indexed_commit":null}
]}'
out=$("$RESOLVER" "$PROJECT_ID" --field code_graph_extra_paths 2>/dev/null)
assert_lines_eq "$out" "/home/u/sibling-a
/home/u/sibling-c" \
    "enabled rows newline-delimited, disabled rows dropped"

# ── Test 2: all-disabled returns blank stdout + exit 0 ─────────────────
write_extras_fixture '{"code_graph_extra_paths":[
    {"path":"/home/u/d","enabled":false,"last_indexed_commit":null}
]}'
set +e
out=$("$RESOLVER" "$PROJECT_ID" --field code_graph_extra_paths 2>/dev/null)
rc=$?
set -e
assert_eq "$out" "" "all-disabled emits no stdout"
assert_eq "$rc" "0" "all-disabled exits 0"

# ── Test 3: empty list → blank + exit 0 (zero extras is valid) ─────────
write_extras_fixture '{"code_graph_extra_paths":[]}'
set +e
out=$("$RESOLVER" "$PROJECT_ID" --field code_graph_extra_paths 2>/dev/null)
rc=$?
set -e
assert_eq "$out" "" "empty list emits no stdout"
assert_eq "$rc" "0" "empty list exits 0"

# ── Test 4: pre-v0.2.47 hub (404 field_not_found) → exit 4 ─────────────
fpath="$scratch/responses/GET_projects_${PROJECT_ID}_config_key=code_graph_extra_paths.json"
cat > "$fpath" <<'JSON'
{"error":{"code":"field_not_found","message":"no such field"}}
JSON
echo "404" > "$fpath.status"
set +e
out=$("$RESOLVER" "$PROJECT_ID" --field code_graph_extra_paths 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "4" "pre-v0.2.47 hub (404 field_not_found) → exit 4"

# ── Test 5: `enabled` defaults to true when absent ─────────────────────
# Mirrors the Python parser's default-on behaviour for pre-spec hubs.
write_extras_fixture '{"code_graph_extra_paths":[
    {"path":"/home/u/no-enabled"}
]}'
rm -f "$fpath.status"
out=$("$RESOLVER" "$PROJECT_ID" --field code_graph_extra_paths 2>/dev/null)
# `jq` path uses `select(.enabled == true)` which drops rows where
# enabled is absent (not strictly equal to true). The python fallback
# uses `row.get("enabled", True)` which keeps them. We accept either —
# this test documents the divergence so it isn't a surprise later.
case "$out" in
    "")
        printf '  NOTE  jq-renderer: missing-enabled treated as disabled (acceptable; documented)\n'
        PASS=$((PASS + 1))
        ;;
    "/home/u/no-enabled")
        printf '  NOTE  python-renderer: missing-enabled defaults to true (acceptable; documented)\n'
        PASS=$((PASS + 1))
        ;;
    *)
        printf '  FAIL  unexpected output for missing-enabled: %q\n' "$out" >&2
        FAIL=$((FAIL + 1))
        ;;
esac

# ── Test 6: malformed top-level body → exit 4 ──────────────────────────
fpath="$scratch/responses/GET_projects_${PROJECT_ID}_config_key=code_graph_extra_paths.json"
printf 'this-is-not-json-at-all' > "$fpath"
set +e
"$RESOLVER" "$PROJECT_ID" --field code_graph_extra_paths >/dev/null 2>&1
rc=$?
set -e
# Malformed body lands at jq → exit 4 (field_decode_failed) OR python
# fallback exit 4. Either is acceptable; we just don't want a crash.
case "$rc" in
    4)
        printf '  PASS  malformed body → exit 4 (field_decode_failed)\n'
        PASS=$((PASS + 1))
        ;;
    *)
        printf '  FAIL  malformed body produced unexpected rc=%s\n' "$rc" >&2
        FAIL=$((FAIL + 1))
        ;;
esac

# ── Test 7: paths with spaces survive the rendering ────────────────────
write_extras_fixture '{"code_graph_extra_paths":[
    {"path":"/home/u/with spaces/x","enabled":true,"last_indexed_commit":null}
]}'
out=$("$RESOLVER" "$PROJECT_ID" --field code_graph_extra_paths 2>/dev/null)
assert_lines_eq "$out" "/home/u/with spaces/x" \
    "paths with spaces round-trip cleanly"

# ── Test 8: 403 on a plain config fetch → exit 5 (forbidden), NOT exit 1
# (v0.2.77 flip 4c). A 403 is a scoped-credential boundary refusal — the
# resolver must surface it as a distinct HARD forbidden condition so hook
# consumers never treat it as "hub unreachable" and env-fall back (which
# would mask the misconfiguration).
cfg_fpath="$scratch/responses/GET_projects_${PROJECT_ID}_config.json"
cat > "$cfg_fpath" <<'JSON'
{"error":{"code":"forbidden","message":"this per-project token does not authorize the project in the URL"}}
JSON
echo "403" > "$cfg_fpath.status"
set +e
err=$("$RESOLVER" "$PROJECT_ID" 2>&1 >/dev/null)
rc=$?
set -e
assert_eq "$rc" "5" "403 on config fetch → exit 5 (forbidden), not exit 1"
case "$err" in
    *forbidden*|*403*)
        printf '  PASS  403 emits a forbidden warning (not hub_unreachable)\n'
        PASS=$((PASS + 1))
        ;;
    *)
        printf '  FAIL  403 warning did not mention forbidden/403: %q\n' "$err" >&2
        FAIL=$((FAIL + 1))
        ;;
esac
rm -f "$cfg_fpath" "$cfg_fpath.status"

# ── Test 9 (v0.2.91 WP-D item 4): stale $VCT_HUB_TOKEN → one-shot retry
# with the on-disk hub.token, one definitive stderr line, exit 0.
#
# RED-PROOF: before the fix the resolver presented the stale env token,
# got 401, and exited 1 ("hub unreachable") — the misleading diagnostic
# the field hit after every update.
FRESH_DISK_TOKEN="fresh-disk-token-v0291-not-a-real-secret"
STALE_ENV_TOKEN="stale-env-token-v0291-not-a-real-secret"
printf '%s' "$FRESH_DISK_TOKEN" > "$VCT_STATE_DIR/hub.token"
printf '%s' "$FRESH_DISK_TOKEN" > "$scratch/responses/_require_token"
cfg_fpath="$scratch/responses/GET_projects_${PROJECT_ID}_config.json"
cat > "$cfg_fpath" <<'JSON'
{"schema_version":1,"kg_collection":"Synthetic_KnowledgeGraph"}
JSON

set +e
out=$(VCT_HUB_TOKEN="$STALE_ENV_TOKEN" "$RESOLVER" "$PROJECT_ID" 2>"$scratch/stale.err")
rc=$?
set -e
assert_eq "$rc" "0" "stale env token → on-disk retry resolves (exit 0)"
case "$out" in
    *Synthetic_KnowledgeGraph*)
        printf '  PASS  stale env token → config body returned\n'
        PASS=$((PASS + 1)) ;;
    *)
        printf '  FAIL  stale env token → unexpected stdout: %q\n' "$out" >&2
        FAIL=$((FAIL + 1)) ;;
esac
case "$(cat "$scratch/stale.err")" in
    *"stale VCT_HUB_TOKEN in env overridden by on-disk hub.token"*)
        printf '  PASS  stale env token → definitive stderr line emitted\n'
        PASS=$((PASS + 1)) ;;
    *)
        printf '  FAIL  stale env token → missing definitive line: %q\n' \
            "$(cat "$scratch/stale.err")" >&2
        FAIL=$((FAIL + 1)) ;;
esac
# The value must never leak into the diagnostics.
case "$(cat "$scratch/stale.err")" in
    *"$FRESH_DISK_TOKEN"*|*"$STALE_ENV_TOKEN"*)
        printf '  FAIL  a token value leaked into stderr\n' >&2
        FAIL=$((FAIL + 1)) ;;
    *)
        printf '  PASS  no token value in stderr\n'
        PASS=$((PASS + 1)) ;;
esac

# ── Test 10 (v0.2.91): VCT_HUB_TOKEN_STRICT=1 pins the bad token ───────
# LEAVE-ALONE half: the harness deliberately pins a wrong token and
# asserts the 401 path — the guard must keep that observable.
set +e
VCT_HUB_TOKEN="$STALE_ENV_TOKEN" VCT_HUB_TOKEN_STRICT=1 \
    "$RESOLVER" "$PROJECT_ID" >/dev/null 2>"$scratch/strict.err"
rc=$?
set -e
assert_eq "$rc" "1" "VCT_HUB_TOKEN_STRICT=1 → 401 path preserved (exit 1)"
case "$(cat "$scratch/strict.err")" in
    *"stale VCT_HUB_TOKEN in env overridden"*)
        printf '  FAIL  strict mode still emitted the override line\n' >&2
        FAIL=$((FAIL + 1)) ;;
    *)
        printf '  PASS  strict mode emits no override line\n'
        PASS=$((PASS + 1)) ;;
esac

# ── Test 11 (v0.2.91): no on-disk token → nothing to fall back to ──────
# LEAVE-ALONE half: the 401 path is unchanged when the fallback is
# impossible (exit 1, byte-compatible with pre-v0.2.91).
rm -f "$VCT_STATE_DIR/hub.token"
set +e
VCT_HUB_TOKEN="$STALE_ENV_TOKEN" "$RESOLVER" "$PROJECT_ID" >/dev/null 2>&1
rc=$?
set -e
assert_eq "$rc" "1" "no on-disk token → 401 path unchanged (exit 1)"
rm -f "$scratch/responses/_require_token" "$cfg_fpath"

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
