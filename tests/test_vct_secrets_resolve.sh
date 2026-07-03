#!/usr/bin/env bash
# Tests for templates/scripts/vct_secrets_resolve.sh.
#
# Strategy: spin up a tiny netcat-driven HTTP fake on a random port,
# point the resolver at it via VCT_HUB_PORT, and assert exit codes +
# stdout. We don't rely on the real launcher hub being available — the
# resolver's contract is "talk HTTP to whatever hub.port advertises",
# and that's what we exercise.
#
# Pre-reqs: bash >= 4, curl, jq OR python3 (the resolver picks whichever).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESOLVER="$REPO_ROOT/templates/scripts/vct_secrets_resolve.sh"
PASS=0
FAIL=0

# ── Mini HTTP fake ──────────────────────────────────────────────────────
#
# Usage: start_fake_hub <route_to_response_dir>. The dir contains files
# named like `GET_projects_<id>_env_key=GITHUB_TOKEN.json` whose content
# is the body to return. Status code comes from a header file with the
# same name + `.status`; default 200.

start_fake_hub() {
    local responses_dir="$1"
    local port_file="$2"
    local pid_file="$3"

    # Find a free port by binding to 0 in Python (always present).
    python3 - <<'PYEOF' >"$port_file"
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PYEOF

    local port
    port=$(cat "$port_file")

    # Tiny fake hub in Python — listens on $port, dispatches based on
    # request line, returns canned responses from $responses_dir.
    #
    # H5 additions:
    #   * Records the Authorization header of every request to
    #     <responses_dir>/last_authorization.txt so tests can assert
    #     "the resolver sent Bearer <token>".
    #   * Honours <responses_dir>/_require_token (one line: the
    #     expected token). If present, requests without a matching
    #     `Authorization: Bearer <token>` get a 401 + JSON envelope.
    #     This emulates the real hub's auth gate.
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
        """Return None if request passes the auth gate, else a (status,body) 401 tuple.

        The auth gate is only enforced when <responses_dir>/_require_token
        exists. When it does, its first line is the expected token; any
        request without `Authorization: Bearer <token>` gets a 401."""
        require_path = os.path.join(responses_dir, "_require_token")
        if not os.path.exists(require_path):
            return None
        with open(require_path) as f:
            expected = f.read().strip()
        auth = self.headers.get("Authorization", "")
        # Canonical form: "Bearer <token>". The real hub uses
        # case-insensitive scheme match; mirror that here.
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
        # Record the Authorization header for assertion in tests.
        # Truncate-and-write so each test gets a clean slate (tests
        # explicitly clear it before exercising the assertion).
        with open(os.path.join(responses_dir, "last_authorization.txt"), "w") as f:
            f.write(self.headers.get("Authorization", ""))

        # Auth gate (only active when _require_token sentinel exists).
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

        # Map "GET /api/v1/projects/abc/env?key=FOO" → file basename:
        # "GET_projects_abc_env_key=FOO". Slashes in the query value
        # (e.g. ?path=/tmp/foo) are also collapsed to underscores so
        # the fixture is a single flat filename.
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
                "error": {"code": "no_fixture", "message": f"no fixture at {body_path}"}
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
    # Wait for socket to accept.
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

scratch="$(mktemp -d)"
trap "stop_fake_hub '$scratch/hub.pid' || true; rm -rf '$scratch'" EXIT
mkdir -p "$scratch/responses"

# ── Auth token (H5, 2026-05-08) ─────────────────────────────────────────
# The resolver now requires `Authorization: Bearer <token>`. We inject
# a canary token via VCT_HUB_TOKEN — the resolver's env override means
# we don't have to scaffold a real ~/.vct/hub.token file.
# An isolated VCT_STATE_DIR ensures any real hub.token on the dev's
# machine doesn't leak into these tests.
export VCT_STATE_DIR="$scratch/state-dir"
mkdir -p "$VCT_STATE_DIR"
HUB_TOKEN_CANARY="canary-bearer-tok-1234567890abcdef1234567890abcdef"
export VCT_HUB_TOKEN="$HUB_TOKEN_CANARY"

# ── File-store isolation (v0.2.73 chain) ────────────────────────────────
# The resolver now falls through hub → file store → project .env. Point
# the file store at an EMPTY scratch dir so (a) the pre-chain exit-code
# tests below keep their tier-1 codes (nothing to fall through to), and
# (b) no test can ever read the developer's real ~/.vct-secrets.
export VCT_SECRETS_DIR="$scratch/secrets-store"
mkdir -p "$VCT_SECRETS_DIR/shared" "$VCT_SECRETS_DIR/projects"

# ── Test 1: hub unreachable → exit 1 ────────────────────────────────────
# Use a port we know nothing is listening on.
no_hub_port=1
# Find one: bind, get port, immediately close — race-y but cheap.
no_hub_port=$(python3 -c '
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0))
print(s.getsockname()[1]); s.close()
')

set +e
out=$(VCT_HUB_PORT="$no_hub_port" "$RESOLVER" some-project-id GITHUB_TOKEN 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "1" "test_vct_secrets_resolve_exits_1_when_hub_unreachable"

# ── Test 1b: token file missing AND env override unset → exit 1 ─────────
# Mirrors the "hub.token doesn't exist yet" path (launcher hasn't
# written it / launcher isn't running). We unset VCT_HUB_TOKEN AND
# point VCT_STATE_DIR at a fresh empty dir so neither lookup wins.
set +e
out=$(VCT_STATE_DIR="$scratch/empty-state-dir" \
      env -u VCT_HUB_TOKEN \
      "$RESOLVER" some-project-id GITHUB_TOKEN 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "1" "test_vct_secrets_resolve_exits_1_when_token_file_missing"

# ── Spin up the fake hub for the rest ───────────────────────────────────
start_fake_hub "$scratch/responses" "$scratch/port" "$scratch/hub.pid"
HUB_PORT=$(cat "$scratch/port")

# ── Test 2: resolver extracts key value from a 200 OK response ──────────
cat >"$scratch/responses/GET_projects_p1_env_key=GITHUB_TOKEN.json" <<'JSON'
{"GITHUB_TOKEN": "ghp_canary123abc"}
JSON

set +e
out=$(VCT_HUB_PORT="$HUB_PORT" "$RESOLVER" p1 GITHUB_TOKEN 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "0" "test_vct_secrets_resolve_extracts_key_from_hub_response/exit_code"
assert_eq "$out" "ghp_canary123abc" "test_vct_secrets_resolve_extracts_key_from_hub_response/value"

# ── Test 3: 404 project_not_found → exit 2 ──────────────────────────────
cat >"$scratch/responses/GET_projects_ghost_env_key=GITHUB_TOKEN.json.status" <<'STATUS'
404
STATUS
cat >"$scratch/responses/GET_projects_ghost_env_key=GITHUB_TOKEN.json" <<'JSON'
{"error": {"code": "project_not_found", "message": "project ghost not found"}}
JSON

set +e
VCT_HUB_PORT="$HUB_PORT" "$RESOLVER" ghost GITHUB_TOKEN 2>/dev/null
rc=$?
set -e
assert_eq "$rc" "2" "test_vct_secrets_resolve_exits_2_when_project_not_found"

# ── Test 4: 404 key_not_active → exit 3 ─────────────────────────────────
cat >"$scratch/responses/GET_projects_p1_env_key=PAUSED_KEY.json.status" <<'STATUS'
404
STATUS
cat >"$scratch/responses/GET_projects_p1_env_key=PAUSED_KEY.json" <<'JSON'
{"error": {"code": "key_not_active", "message": "key PAUSED_KEY paused"}}
JSON

set +e
VCT_HUB_PORT="$HUB_PORT" "$RESOLVER" p1 PAUSED_KEY 2>/dev/null
rc=$?
set -e
assert_eq "$rc" "3" "test_vct_secrets_resolve_exits_3_when_key_not_active"

# ── Test 5: by-path resolution, then read ───────────────────────────────
# Encode the test path the way the resolver does.
test_folder="/tmp/test-folder-$$"
encoded_folder=$(python3 -c '
import urllib.parse, sys
print(urllib.parse.quote(sys.argv[1], safe=""))
' "$test_folder")
# The resolver uses url_encode() with safe set [a-zA-Z0-9._~/-], so /
# stays unencoded. Match that for the fixture filename. The fake-hub
# fixture key is everything after `/api/v1/` with `/` → `_`, `?` → `_`,
# so the by-path query becomes:
#   GET /api/v1/projects/by-path?path=/tmp/test-folder-$$
#   → key projects_by-path with query path=/tmp/test-folder-$$
# After our handler's path-and-query mash:
#   "projects_by-path_path=/tmp/test-folder-$$"
# But `/` in the QUERY VALUE is preserved (it's a literal slash, fully
# replaced in the substitution after the `?` split). The handler
# replaces the path-side '/' with '_'; the query is appended verbatim.
qkey="projects_by-path_path=${test_folder}"
# Substitute literal `/` in the query value to whatever the resolver
# sends — bash's url_encode keeps `/` unescaped.
fixture_name="GET_${qkey//\//\/}.json"
# But filenames can't contain `/`, so the fake hub stores them with `/`
# replaced by `\` only inside the path side, not the query — and the
# fake's key construction already does `.replace("/", "_")` on the
# WHOLE path_key, INCLUDING the query. So:
qkey_filename="$(echo "$qkey" | tr '/' '_')"
fixture_name="GET_${qkey_filename}.json"

cat >"$scratch/responses/${fixture_name}" <<JSON
{"id": "p-resolved-id", "folder_path": "${test_folder}"}
JSON

# Then the env lookup once we have the id:
cat >"$scratch/responses/GET_projects_p-resolved-id_env_key=API_KEY.json" <<'JSON'
{"API_KEY": "sk-canary-abc"}
JSON

set +e
out=$(VCT_HUB_PORT="$HUB_PORT" "$RESOLVER" "$test_folder" API_KEY 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "0" "test_vct_secrets_resolve_resolves_path_then_reads_key/exit_code"
assert_eq "$out" "sk-canary-abc" "test_vct_secrets_resolve_resolves_path_then_reads_key/value"

# ── Test 6: resolve-project subcommand ──────────────────────────────────
set +e
out=$(VCT_HUB_PORT="$HUB_PORT" "$RESOLVER" resolve-project "$test_folder" 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "0" "test_vct_secrets_resolve_subcommand_resolve_project/exit_code"
assert_eq "$out" "p-resolved-id" "test_vct_secrets_resolve_subcommand_resolve_project/value"

# ── Test 7 (H5): resolver sends Authorization: Bearer <token> ───────────
# This is the keystone test for the hub auth-token gate. We assert two
# things:
#   (a) the resolver passes through `Authorization: Bearer $VCT_HUB_TOKEN`
#       on a successful 200 path, and
#   (b) when the fake hub has the `_require_token` sentinel set to the
#       same token, the resolver succeeds — i.e. the gate only lets
#       authenticated requests through.
# The fake hub records every Authorization header to
# `<responses>/last_authorization.txt` so we can read it back.
rm -f "$scratch/responses/last_authorization.txt"
set +e
out=$(VCT_HUB_PORT="$HUB_PORT" "$RESOLVER" p1 GITHUB_TOKEN 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "0" "test_vct_secrets_resolve_sends_authorization_header/exit_code"
assert_eq "$out" "ghp_canary123abc" "test_vct_secrets_resolve_sends_authorization_header/value"
got_auth=""
if [[ -f "$scratch/responses/last_authorization.txt" ]]; then
    got_auth=$(<"$scratch/responses/last_authorization.txt")
fi
assert_eq "$got_auth" "Bearer $HUB_TOKEN_CANARY" \
    "test_vct_secrets_resolve_sends_authorization_header/header_value"

# ── Test 8 (H5): hub gate enforced — wrong token → 401 → exit 1 ─────────
# Activate the fake hub's auth gate and point the resolver at a token
# that doesn't match. We expect exit 1 (mapped from 401) — see the
# resolver's note on stale-token semantics.
echo -n "$HUB_TOKEN_CANARY" >"$scratch/responses/_require_token"
set +e
VCT_HUB_PORT="$HUB_PORT" VCT_HUB_TOKEN="wrong-token-$$" \
    "$RESOLVER" p1 GITHUB_TOKEN 2>/dev/null
rc=$?
set -e
assert_eq "$rc" "1" "test_vct_secrets_resolve_exits_1_on_401"

# ── Test 9 (H5): hub gate enforced — right token → success ─────────────
# Sanity check: with the gate active and the correct token, the same
# request still succeeds. This pins the contract end-to-end.
set +e
out=$(VCT_HUB_PORT="$HUB_PORT" VCT_HUB_TOKEN="$HUB_TOKEN_CANARY" \
        "$RESOLVER" p1 GITHUB_TOKEN 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "0" "test_vct_secrets_resolve_passes_gate_with_correct_token/exit_code"
assert_eq "$out" "ghp_canary123abc" "test_vct_secrets_resolve_passes_gate_with_correct_token/value"

# Tidy up the gate sentinel for any future tests appended below.
rm -f "$scratch/responses/_require_token"

# ── Test 10 (H5): token reads from file when env var unset ──────────────
# The resolver's documented order is env → state-dir/hub.token. Pull
# the env override out, write the token to the state-dir, and confirm
# the request still authenticates correctly.
file_token="canary-from-file-$$-abcdef"
echo -n "$file_token" >"$scratch/state-dir/hub.token"
chmod 600 "$scratch/state-dir/hub.token" || true
echo -n "$file_token" >"$scratch/responses/_require_token"
rm -f "$scratch/responses/last_authorization.txt"

set +e
out=$(env -u VCT_HUB_TOKEN VCT_HUB_PORT="$HUB_PORT" VCT_STATE_DIR="$scratch/state-dir" \
        "$RESOLVER" p1 GITHUB_TOKEN 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "0" "test_vct_secrets_resolve_reads_token_from_file/exit_code"
assert_eq "$out" "ghp_canary123abc" "test_vct_secrets_resolve_reads_token_from_file/value"
got_auth=""
if [[ -f "$scratch/responses/last_authorization.txt" ]]; then
    got_auth=$(<"$scratch/responses/last_authorization.txt")
fi
assert_eq "$got_auth" "Bearer $file_token" \
    "test_vct_secrets_resolve_reads_token_from_file/header_value"

# Tidy up so subsequent tests (none today, but future-proof) get clean
# state.
rm -f "$scratch/responses/_require_token"
rm -f "$scratch/state-dir/hub.token"

# ════════════════════════════════════════════════════════════════════════
# v0.2.73 resolution chain: hub → file store → project .env
# Synthetic fixture shared with the pytest + ps1 siblings
# (tests/test_agent_secrets.py, tests/test_vct_secrets_resolve_ps1.py) —
# same key names, same values, same parse cases.
# ════════════════════════════════════════════════════════════════════════

# ── Test 11: tier 2 — file store resolves when hub unreachable ──────────
echo -n "synthetic-file-store-value" >"$VCT_SECRETS_DIR/shared/EXAMPLE_API_TOKEN"
set +e
out=$(VCT_HUB_PORT="$no_hub_port" "$RESOLVER" some-project-id EXAMPLE_API_TOKEN 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "0" "test_chain_tier2_file_store_when_hub_unreachable/exit_code"
assert_eq "$out" "synthetic-file-store-value" "test_chain_tier2_file_store_when_hub_unreachable/value"

# ── Test 12: tier 2 — key_not_active (hub up) still falls to store ──────
# Fake hub says PAUSED_KEY is key_not_active; the file store has an
# independent copy (the launcher gate governs keychain slots only).
echo -n "synthetic-paused-but-in-store" >"$VCT_SECRETS_DIR/shared/PAUSED_KEY"
set +e
out=$(VCT_HUB_PORT="$HUB_PORT" "$RESOLVER" p1 PAUSED_KEY 2>/dev/null)
rc=$?
set -e
assert_eq "$rc" "0" "test_chain_key_not_active_falls_to_file_store/exit_code"
assert_eq "$out" "synthetic-paused-but-in-store" "test_chain_key_not_active_falls_to_file_store/value"
rm -f "$VCT_SECRETS_DIR/shared/PAUSED_KEY"

# ── Test 13: tier 3 — project .env resolves when tiers 1+2 miss ────────
proj_dir="$scratch/proj-with-dotenv"
mkdir -p "$proj_dir"
cat >"$proj_dir/.env" <<'DOTENV'
# comment line — skipped
export EXPORTED_KEY=plain-exported
QUOTED_KEY="double quoted value"
SINGLE_KEY='single quoted value'
FIRST_MATCH=first-wins
FIRST_MATCH=second-loses
NO_EXPANSION=$HOME/literal
MISMATCHED='half"
DOTENV

for case_kv in \
    "EXPORTED_KEY:plain-exported" \
    "QUOTED_KEY:double quoted value" \
    "SINGLE_KEY:single quoted value" \
    "FIRST_MATCH:first-wins" \
    'NO_EXPANSION:$HOME/literal' \
    "MISMATCHED:'half\""; do
    ckey="${case_kv%%:*}"
    cwant="${case_kv#*:}"
    set +e
    out=$(VCT_HUB_PORT="$no_hub_port" "$RESOLVER" "$proj_dir" "$ckey" 2>/dev/null)
    rc=$?
    set -e
    assert_eq "$rc" "0" "test_chain_tier3_dotenv_parse/${ckey}/exit_code"
    assert_eq "$out" "$cwant" "test_chain_tier3_dotenv_parse/${ckey}/value"
done

# ── Test 14: tier 3 skipped for a bare project id (no folder known) ────
# The tier-1 exit code (1: hub unreachable) is preserved, and the
# diagnostic explains why .env was not consulted.
set +e
stderr_out=$(VCT_HUB_PORT="$no_hub_port" "$RESOLVER" bare-project-id EXPORTED_KEY 2>&1 >/dev/null)
rc=$?
set -e
assert_eq "$rc" "1" "test_chain_tier3_skipped_for_bare_id/exit_code"
case "$stderr_out" in
    *"tier 3 (.env) skipped"*)
        assert_eq "yes" "yes" "test_chain_tier3_skipped_for_bare_id/diagnostic" ;;
    *)
        assert_eq "$stderr_out" "contains 'tier 3 (.env) skipped'" "test_chain_tier3_skipped_for_bare_id/diagnostic" ;;
esac

# ── Test 15: all-miss preserves tier-1 exit code (3 = key_not_active) ───
# PAUSED_KEY was removed from the store above; the .env-less folder arg
# means tier 3 misses too. Exit must be the historical 3 — only now it
# means "missed everywhere".
no_env_dir="$scratch/proj-no-dotenv"
mkdir -p "$no_env_dir"
# Register the folder→project mapping so tier 1 reaches the env call.
qkey_no_env="$(echo "projects_by-path_path=${no_env_dir}" | tr '/' '_')"
cat >"$scratch/responses/GET_${qkey_no_env}.json" <<JSON
{"id": "p1", "folder_path": "${no_env_dir}"}
JSON
set +e
VCT_HUB_PORT="$HUB_PORT" "$RESOLVER" "$no_env_dir" PAUSED_KEY 2>/dev/null
rc=$?
set -e
assert_eq "$rc" "3" "test_chain_all_miss_preserves_tier1_exit_code"

# ── Test 16: never-print-value — a miss must not leak OTHER values ─────
# The .env + store contain synthetic values; asking for a MISSING key
# must not echo any of them on stderr (errors name keys + tiers only).
set +e
stderr_out=$(VCT_HUB_PORT="$no_hub_port" "$RESOLVER" "$proj_dir" TOTALLY_MISSING_KEY 2>&1 >/dev/null)
rc=$?
set -e
[[ $rc -ne 0 ]] || assert_eq "$rc" "nonzero" "test_chain_never_prints_values/miss_is_nonzero"
leak="no"
case "$stderr_out" in
    *"synthetic-file-store-value"*|*"plain-exported"*|*"double quoted value"*|*"single quoted value"*) leak="yes" ;;
esac
assert_eq "$leak" "no" "test_chain_never_prints_values/no_value_in_stderr"

# ── Summary ─────────────────────────────────────────────────────────────
printf '\n%s\n' "── Summary: $PASS passed, $FAIL failed"
exit $((FAIL > 0 ? 1 : 0))
