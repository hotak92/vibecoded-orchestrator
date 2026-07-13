#!/usr/bin/env bash
# Pure-Bash test suite for the vct CLI. No external deps (no bats).
# Uses VCT_SECRETS_DIR override so tests never touch the real ~/.vct-secrets.
#
# Usage: ./test_vct.sh
# Exits non-zero if any test fails.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
VCT="$HERE/../vct"

if [ ! -x "$VCT" ]; then
    printf 'FAIL: vct not executable at %s\n' "$VCT" >&2
    exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export VCT_SECRETS_DIR="$TMP/store"

PASS=0
FAIL=0
FAILED_TESTS=()

ok() { printf '  ok  %s\n' "$1"; PASS=$((PASS+1)); }
ko() { printf '  FAIL %s — %s\n' "$1" "${2:-}" >&2; FAIL=$((FAIL+1)); FAILED_TESTS+=("$1"); }

run_test() {
    local name=$1; shift
    printf -- '--- %s\n' "$name"
    if "$@"; then ok "$name"
    else ko "$name" "exit=$?"
    fi
}

# Helper: assert substring in file
assert_contains_file() {
    local f=$1 needle=$2 msg=${3:-}
    if grep -qF -- "$needle" "$f"; then return 0
    else printf '    expected %s in %s (%s)\n' "$needle" "$f" "$msg" >&2; return 1
    fi
}

# Helper: assert absent
assert_absent_file() {
    local f=$1 needle=$2
    if grep -qF -- "$needle" "$f"; then
        printf '    unexpected %s in %s\n' "$needle" "$f" >&2; return 1
    fi
    return 0
}

# --- Test: help works ---
t_help() {
    "$VCT" help > "$TMP/help.out" 2>&1 || return 1
    grep -q "list" "$TMP/help.out" || return 1
    grep -q "exec" "$TMP/help.out" || return 1
    grep -q "migrate-from-env" "$TMP/help.out" || return 1
}

# --- Test: set reads stdin, refuses argv --value ---
t_set_refuses_value_argv() {
    if "$VCT" set --project demo --key API_KEY --value secret123 2>"$TMP/err" </dev/null; then
        return 1
    fi
    grep -q "forbidden" "$TMP/err" || return 1
}

# --- Test: set stores via stdin, chmod 600 ---
t_set_stdin() {
    printf 'topsecretvalue' | "$VCT" set --project demo --key API_KEY 2>/dev/null || return 1
    local f="$VCT_SECRETS_DIR/projects/demo/API_KEY"
    [ -f "$f" ] || return 1
    local mode
    mode=$(stat -c '%a' "$f")
    [ "$mode" = "600" ] || { echo "    mode=$mode not 600"; return 1; }
    [ "$(cat "$f")" = "topsecretvalue" ] || return 1
}

# --- Test: list shows keys, never values ---
t_list() {
    printf 'v1' | "$VCT" set --project demo --key KEY_A 2>/dev/null
    printf 'v2-sensitive' | "$VCT" set --project demo --key KEY_B 2>/dev/null
    "$VCT" list --project demo > "$TMP/list.out" 2>/dev/null || return 1
    grep -q "^KEY_A$" "$TMP/list.out" || return 1
    grep -q "^KEY_B$" "$TMP/list.out" || return 1
    if grep -q "v2-sensitive" "$TMP/list.out"; then return 1; fi
}

# --- Test: exec injects env into child, parent unchanged ---
t_exec_injects() {
    printf 'injected-val' | "$VCT" set --project demo --key TOKEN 2>/dev/null
    # Child should see TOKEN
    local out
    out=$("$VCT" exec --project demo --secret TOKEN -- bash -c 'printf %s "$TOKEN"' 2>/dev/null)
    [ "$out" = "injected-val" ] || { echo "    got: $out"; return 1; }
    # Parent env must not have TOKEN set by us
    if [ -n "${TOKEN-}" ]; then echo "    parent TOKEN leaked"; return 1; fi
}

# --- Test: exec KEY=VAR renaming ---
t_exec_rename() {
    printf 'pat123' | "$VCT" set --project demo --key github_pat 2>/dev/null
    local out
    out=$("$VCT" exec --project demo --secret github_pat=GH_TOKEN -- bash -c 'printf %s "$GH_TOKEN"' 2>/dev/null)
    [ "$out" = "pat123" ] || return 1
    # Source-name var should NOT be set in child
    out=$("$VCT" exec --project demo --secret github_pat=GH_TOKEN -- bash -c 'printf %s "${github_pat-UNSET}"' 2>/dev/null)
    [ "$out" = "UNSET" ] || return 1
}

# --- Test: exec multiple secrets ---
t_exec_multiple() {
    printf 'aaa' | "$VCT" set --project demo --key KEY1 2>/dev/null
    printf 'bbb' | "$VCT" set --project demo --key KEY2 2>/dev/null
    local out
    out=$("$VCT" exec --project demo --secret KEY1 --secret KEY2=RENAMED -- bash -c 'printf "%s:%s" "$KEY1" "$RENAMED"' 2>/dev/null)
    [ "$out" = "aaa:bbb" ] || return 1
}

# --- Test: resolution order project > shared ---
t_resolution_order() {
    mkdir -p "$VCT_SECRETS_DIR/shared"
    printf 'SHARED_VAL' > "$VCT_SECRETS_DIR/shared/DUAL"
    chmod 600 "$VCT_SECRETS_DIR/shared/DUAL"
    # No project version yet — should fall back to shared
    local out
    out=$("$VCT" exec --project demo --secret DUAL -- bash -c 'printf %s "$DUAL"' 2>/dev/null)
    [ "$out" = "SHARED_VAL" ] || { echo "    shared fallback got: $out"; return 1; }
    # Now add project version — should win
    printf 'PROJECT_VAL' | "$VCT" set --project demo --key DUAL 2>/dev/null
    out=$("$VCT" exec --project demo --secret DUAL -- bash -c 'printf %s "$DUAL"' 2>/dev/null)
    [ "$out" = "PROJECT_VAL" ] || { echo "    project override got: $out"; return 1; }
}

# --- Test: missing secret → exit 2, child NOT run ---
t_exec_missing_fails_fast() {
    # Sentinel file to detect if child ran
    local sentinel="$TMP/sentinel-$$"
    rm -f "$sentinel"
    "$VCT" exec --project demo --secret DOES_NOT_EXIST -- touch "$sentinel" 2>"$TMP/err"
    local ec=$?
    [ $ec -eq 2 ] || { echo "    expected exit 2, got $ec"; return 1; }
    if [ -f "$sentinel" ]; then echo "    child executed despite missing secret"; return 1; fi
    grep -q "DOES_NOT_EXIST" "$TMP/err" || return 1
}

# --- Test: revoke removes file ---
t_revoke() {
    printf 'x' | "$VCT" set --project demo --key TMPKEY 2>/dev/null
    [ -f "$VCT_SECRETS_DIR/projects/demo/TMPKEY" ] || return 1
    "$VCT" revoke --project demo --key TMPKEY --yes 2>/dev/null || return 1
    [ ! -f "$VCT_SECRETS_DIR/projects/demo/TMPKEY" ] || return 1
}

# --- Test: path traversal rejected ---
t_path_injection() {
    if printf 'x' | "$VCT" set --project demo --key "../escape" 2>/dev/null; then return 1; fi
    if printf 'x' | "$VCT" set --project "../bad" --key KEY 2>/dev/null; then return 1; fi
    if printf 'x' | "$VCT" set --project "demo/sub" --key KEY 2>/dev/null; then return 1; fi
}

# --- Test: migrate-from-env parses typical .env ---
t_migrate_from_env() {
    local env="$TMP/sample.env"
    cat > "$env" <<'EOF'
# a comment
FOO=bar
BAZ="quoted value"
QUX='single quoted'
export EXPORTED=value3

# blank line above

INVALID LINE WITHOUT EQUALS
EMPTY=
EOF
    "$VCT" migrate-from-env "$env" --project mproj 2>/dev/null || return 1
    [ -f "$VCT_SECRETS_DIR/projects/mproj/FOO" ] || return 1
    [ "$(cat "$VCT_SECRETS_DIR/projects/mproj/FOO")" = "bar" ] || return 1
    [ "$(cat "$VCT_SECRETS_DIR/projects/mproj/BAZ")" = "quoted value" ] || return 1
    [ "$(cat "$VCT_SECRETS_DIR/projects/mproj/QUX")" = "single quoted" ] || return 1
    [ "$(cat "$VCT_SECRETS_DIR/projects/mproj/EXPORTED")" = "value3" ] || return 1
    # Source renamed
    [ ! -f "$env" ] || return 1
    [ -f "$env.migrated" ] || return 1
    # chmod 600 on imported
    local mode
    mode=$(stat -c '%a' "$VCT_SECRETS_DIR/projects/mproj/FOO")
    [ "$mode" = "600" ] || return 1
}

# --- Test: migrate-from-env --dry-run leaves source intact ---
t_migrate_dry_run() {
    local env="$TMP/sample2.env"
    printf 'AAA=bbb\n' > "$env"
    "$VCT" migrate-from-env "$env" --project drytest --dry-run >"$TMP/dry.out" 2>&1 || return 1
    [ -f "$env" ] || return 1  # source untouched
    [ ! -f "$VCT_SECRETS_DIR/projects/drytest/AAA" ] || return 1
    grep -q "would import" "$TMP/dry.out" || return 1
}

# --- Test: audit log gets entries, no values ---
t_audit_log() {
    printf 'SECRET_VALUE_XYZ' | "$VCT" set --project auditp --key K1 2>/dev/null
    "$VCT" exec --project auditp --secret K1 -- true 2>/dev/null
    local log="$VCT_SECRETS_DIR/audit.log"
    [ -f "$log" ] || return 1
    grep -q '"op":"set"' "$log" || return 1
    grep -q '"op":"exec"' "$log" || return 1
    grep -q '"project":"auditp"' "$log" || return 1
    # Must NOT contain the actual value
    if grep -q "SECRET_VALUE_XYZ" "$log"; then echo "    VALUE LEAKED IN AUDIT"; return 1; fi
}

# --- Test: doctor fixes wrong perms ---
t_doctor() {
    printf 'x' | "$VCT" set --project docp --key DKEY 2>/dev/null
    local f="$VCT_SECRETS_DIR/projects/docp/DKEY"
    chmod 644 "$f"
    local mode
    mode=$(stat -c '%a' "$f")
    [ "$mode" = "644" ] || return 1
    "$VCT" doctor 2>/dev/null
    mode=$(stat -c '%a' "$f")
    [ "$mode" = "600" ] || { echo "    mode after doctor=$mode"; return 1; }
}

# --- Test: detect-project walks up ---
t_detect_project() {
    mkdir -p "$TMP/proj/sub/deep"
    printf 'MyProject\n' > "$TMP/proj/.vct-project"
    local out
    out=$(cd "$TMP/proj/sub/deep" && "$VCT" detect-project 2>/dev/null)
    [ "$out" = "MyProject" ] || { echo "    got: $out"; return 1; }
}

# --- Test: get refuses TTY without --trusted (simulated: stdout non-TTY returns value) ---
t_get_non_tty() {
    printf 'val1' | "$VCT" set --project gp --key K 2>/dev/null
    # stdout redirected to file → non-TTY → should work without --trusted
    local out
    out=$("$VCT" get --project gp --key K 2>/dev/null)
    [ "$out" = "val1" ] || return 1
}

# --- Test: copy between projects ---
t_copy() {
    printf 'copyval' | "$VCT" set --project src_p --key CKEY 2>/dev/null
    "$VCT" copy --from-project src_p --to-project dst_p --key CKEY --yes 2>/dev/null || return 1
    [ "$(cat "$VCT_SECRETS_DIR/projects/dst_p/CKEY")" = "copyval" ] || return 1
}

# --- Test (v0.2.54 S-2): get/exec default to shared scope without --project ---
t_shared_default_get_exec() {
    mkdir -p "$VCT_SECRETS_DIR/shared"
    printf 'shared-default-val' > "$VCT_SECRETS_DIR/shared/S2KEY"
    chmod 600 "$VCT_SECRETS_DIR/shared/S2KEY"
    local out
    out=$("$VCT" get --key S2KEY 2>/dev/null) || return 1
    [ "$out" = "shared-default-val" ] || { echo "    get got: $out"; return 1; }
    out=$("$VCT" exec --secret S2KEY=S2VAR -- bash -c 'printf %s "$S2VAR"' 2>/dev/null) || return 1
    [ "$out" = "shared-default-val" ] || { echo "    exec got: $out"; return 1; }
}

# --- Test (v0.2.54 S-5): can-read exit codes, silent output ---
t_can_read() {
    mkdir -p "$VCT_SECRETS_DIR/shared"
    printf 'x' > "$VCT_SECRETS_DIR/shared/CR_KEY"
    chmod 600 "$VCT_SECRETS_DIR/shared/CR_KEY"
    "$VCT" can-read --key CR_KEY 2>/dev/null || return 1
    local out
    out=$("$VCT" can-read --key CR_KEY 2>/dev/null)
    [ -z "$out" ] || { echo "    can-read printed: $out"; return 1; }
    if "$VCT" can-read --key NOPE_MISSING 2>/dev/null; then
        echo "    can-read exit 0 on missing key"; return 1
    fi
}

# --- Test (v0.2.54 S-5): resolve prints source path, project wins over shared ---
t_resolve_path() {
    mkdir -p "$VCT_SECRETS_DIR/shared"
    printf 'sv' > "$VCT_SECRETS_DIR/shared/RKEY"
    chmod 600 "$VCT_SECRETS_DIR/shared/RKEY"
    local out
    out=$("$VCT" resolve --key RKEY 2>/dev/null) || return 1
    [ "$out" = "$VCT_SECRETS_DIR/shared/RKEY" ] || { echo "    got: $out"; return 1; }
    printf 'pv' | "$VCT" set --project rp --key RKEY 2>/dev/null
    out=$("$VCT" resolve --project rp --key RKEY 2>/dev/null) || return 1
    [ "$out" = "$VCT_SECRETS_DIR/projects/rp/RKEY" ] || { echo "    got: $out"; return 1; }
    if "$VCT" resolve --key TOTALLY_MISSING 2>/dev/null; then
        echo "    resolve exit 0 on missing key"; return 1
    fi
}

# --- Test (v0.2.54 S-3/S-4 sibling): doctor warns on malformed github_pat ---
t_doctor_token_shape() {
    mkdir -p "$VCT_SECRETS_DIR/shared"
    # Valid classic PAT shape → no token-shape warning for this file
    printf 'ghp_%s' "$(printf 'a%.0s' $(seq 1 36))" > "$VCT_SECRETS_DIR/shared/github_pat"
    chmod 600 "$VCT_SECRETS_DIR/shared/github_pat"
    "$VCT" doctor 2> "$TMP/doctor1.err" || return 1
    if grep -q "token-shape:.*shared/github_pat " "$TMP/doctor1.err"; then
        echo "    valid ghp_ shape flagged"; return 1
    fi
    # Corrupted blob under a github_pat name → warned
    printf 'not-a-real-token-blob-of-junk' > "$VCT_SECRETS_DIR/shared/github_pat.alt"
    chmod 600 "$VCT_SECRETS_DIR/shared/github_pat.alt"
    "$VCT" doctor 2> "$TMP/doctor2.err" || return 1
    grep -q "token-shape" "$TMP/doctor2.err" || { echo "    junk shape not flagged"; return 1; }
    # Warning must never leak the value
    if grep -q "not-a-real-token-blob-of-junk" "$TMP/doctor2.err"; then
        echo "    doctor leaked secret content"; return 1
    fi
    rm -f "$VCT_SECRETS_DIR/shared/github_pat" "$VCT_SECRETS_DIR/shared/github_pat.alt"
}

# --- Fake-hub helper (v0.2.77 Part 8 Task 4b probe tests) ------------------
# Spins a tiny python3 HTTP server that answers /env with a fixed status,
# writes hub.port + hub.token under an isolated VCT_STATE_DIR, and returns
# the state dir. The caller sets the desired status via arg $2.
_start_fake_hub() {
    local status="$1" statedir="$2"
    command -v python3 >/dev/null 2>&1 || return 2  # skip if no python3
    mkdir -p "$statedir"
    local portfile="$statedir/.port"
    python3 - "$status" "$portfile" <<'PYEOF' &
import http.server, sys, socket
status = int(sys.argv[1]); portfile = sys.argv[2]
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
with open(portfile, "w") as f: f.write(str(port))
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = b'{"error":{"code":"forbidden"}}' if status != 200 else b'{"K":"v"}'
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PYEOF
    echo $! > "$statedir/.hubpid"
    # Wait for the port file, then publish hub.port + a global hub.token.
    local port=""
    for _ in $(seq 1 40); do
        [ -s "$portfile" ] && port=$(cat "$portfile") && break
        sleep 0.05
    done
    [ -n "$port" ] || return 1
    printf '%s' "$port" > "$statedir/hub.port"
    printf 'global-canary-token' > "$statedir/hub.token"
    chmod 600 "$statedir/hub.token"
    # Readiness probe.
    for _ in $(seq 1 40); do
        curl -s --max-time 0.5 "http://127.0.0.1:${port}/api/v1/projects/x/env?key=K" >/dev/null 2>&1 && break
        sleep 0.05
    done
    return 0
}

_stop_fake_hub() {
    local statedir="$1"
    [ -f "$statedir/.hubpid" ] && kill "$(cat "$statedir/.hubpid")" 2>/dev/null || true
}

# --- Test (4b): a 403 probe must NOT suggest `vct set`; point at resolver ---
# Post-flip the probe rides the global hub.token (the scoped file is keyed
# by launcher project_id, not the file-store name) and /env returns 403.
# The miss message must withhold the divergent-copy `vct set` hint.
t_die_miss_403_withholds_vct_set() {
    local sd="$TMP/hub403"
    _start_fake_hub 403 "$sd"; local rc=$?
    [ "$rc" -eq 2 ] && { echo "    (skipped: no python3)"; return 0; }
    [ "$rc" -eq 0 ] || { echo "    fake hub failed to start"; return 1; }
    local err
    # `bare-miss-proj` has no file-store secret → die_miss fires. Isolated
    # VCT_STATE_DIR points the probe at the fake 403 hub.
    err=$(VCT_STATE_DIR="$sd" VCT_HUB_PORT="$(cat "$sd/hub.port")" \
          "$VCT" get --project bare-miss-proj --key SOMEKEY 2>&1 >/dev/null)
    _stop_fake_hub "$sd"
    # Must NOT recommend `vct set` on a 403 (the harmful divergent-copy path).
    # The HARMFUL recommendation is the "Fix: vct set ..." line (the
    # classic miss hint). The correct messages instead say "Do NOT 'vct
    # set' it", so we key on the recommendation phrase, not any mention.
    case "$err" in
        *"Fix: vct set"*) echo "    403 miss wrongly RECOMMENDED 'vct set': $err"; return 1 ;;
    esac
    # Must point at the resolver instead.
    case "$err" in
        *vct_secrets_resolve.sh*|*agent_secrets*) : ;;
        *) echo "    403 miss did not point at the resolver: $err"; return 1 ;;
    esac
}

# --- Test (4b): a confirmed 200 HIT still points at the resolver, no `vct set`
t_die_miss_hit_points_at_resolver() {
    local sd="$TMP/hub200"
    _start_fake_hub 200 "$sd"; local rc=$?
    [ "$rc" -eq 2 ] && { echo "    (skipped: no python3)"; return 0; }
    [ "$rc" -eq 0 ] || { echo "    fake hub failed to start"; return 1; }
    local err
    err=$(VCT_STATE_DIR="$sd" VCT_HUB_PORT="$(cat "$sd/hub.port")" \
          "$VCT" get --project hit-proj --key K 2>&1 >/dev/null)
    _stop_fake_hub "$sd"
    case "$err" in
        *"Fix: vct set"*) echo "    hit miss wrongly RECOMMENDED 'vct set': $err"; return 1 ;;
    esac
    case "$err" in
        *vct_secrets_resolve.sh*) : ;;
        *) echo "    hit miss did not point at the resolver: $err"; return 1 ;;
    esac
}

# =========================================================================
# v0.2.80 Part A/B/C — secret-shape write-guard + recover-blob + doctor taxonomy
# =========================================================================

MIGRATE_SHARED="$HERE/../migrate-shared.sh"
SHAPE_LIB="$HERE/../lib/secret_shape.sh"
# Repo-root tests/fixtures/ — HERE is tools/vct-secrets/tests/.
FIXTURE="$HERE/../../../tests/fixtures/secret_value_shape_parity.json"

# --- Test: the shared predicate library matches the parity fixture (bash leg) ---
# This is the bash third of the three-runner parity (Python SSOT + Rust + bash),
# locking tools/vct-secrets/lib/secret_shape.sh::_is_single_line_secret /
# _classify_secret_value to vco_lib/secret_value_shape.py via the shared fixture.
t_shape_parity_fixture() {
    command -v python3 >/dev/null 2>&1 || { echo "    (skipped: no python3)"; return 0; }
    [ -r "$SHAPE_LIB" ]  || { echo "    shape lib missing at $SHAPE_LIB"; return 1; }
    [ -r "$FIXTURE" ]    || { echo "    fixture missing at $FIXTURE"; return 1; }
    # shellcheck source=../lib/secret_shape.sh
    . "$SHAPE_LIB"

    local n
    n=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["cases"]))' "$FIXTURE") || return 1
    [ "$n" -ge 10 ] || { echo "    fixture has only $n cases (expected >= 10)"; return 1; }

    # Guard: the required named cases must all be present (test_fixture_covers_
    # named_cases equivalent). Missing a case = silent parity gap.
    local required="single_line_valid embedded_lf embedded_crlf post_line0_key_eq_blob \
export_key_eq_blob github_pat_over_200 pem_private_key_legit indented_json_with_eq_not_a_blob \
base64_padding_not_a_blob line0_is_key_eq"
    local names
    names=$(python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(open(sys.argv[1]))["cases"]))' "$FIXTURE")
    local want
    for want in $required; do
        case " $names " in *" $want "*) : ;; *) echo "    missing required case: $want"; return 1 ;; esac
    done

    local i=0 fail=0
    while [ "$i" -lt "$n" ]; do
        local name key eok ereason etax value reason gok gokbit tax
        name=$(python3    -c 'import json,sys; print(json.load(open(sys.argv[1]))["cases"][int(sys.argv[2])]["name"])' "$FIXTURE" "$i")
        key=$(python3     -c 'import json,sys; print(json.load(open(sys.argv[1]))["cases"][int(sys.argv[2])]["key_name"])' "$FIXTURE" "$i")
        eok=$(python3     -c 'import json,sys; print("1" if json.load(open(sys.argv[1]))["cases"][int(sys.argv[2])]["expect_ok"] else "0")' "$FIXTURE" "$i")
        ereason=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cases"][int(sys.argv[2])]["expect_reason"])' "$FIXTURE" "$i")
        etax=$(python3    -c 'import json,sys; print(json.load(open(sys.argv[1]))["cases"][int(sys.argv[2])]["expect_taxonomy"])' "$FIXTURE" "$i")
        # base64-encode the value so ALL bytes (embedded newlines, controls,
        # `==` padding) survive the shell round-trip intact.
        value=$(python3 -c 'import json,sys,base64; sys.stdout.write(base64.b64encode(json.load(open(sys.argv[1]))["cases"][int(sys.argv[2])]["value"].encode()).decode())' "$FIXTURE" "$i" | base64 -d)

        reason=$(_is_single_line_secret "$value" "$key" 0); gok=$?
        tax=$(_classify_secret_value "$value" "$key")
        gokbit=$([ "$gok" -eq 0 ] && echo 1 || echo 0)
        [ "$gokbit" = "1" ] && reason=""   # accept → empty reason

        if [ "$gokbit" != "$eok" ] || [ "$reason" != "$ereason" ] || [ "$tax" != "$etax" ]; then
            fail=$((fail+1))
            printf '    PARITY FAIL %s: ok=%s/%s reason=[%s]/[%s] tax=[%s]/[%s]\n' \
                "$name" "$gokbit" "$eok" "$reason" "$ereason" "$tax" "$etax" >&2
        fi
        i=$((i+1))
    done
    [ "$fail" -eq 0 ]
}

# --- Test: cmd_set rejects a blob, accepts single-line/PEM/allow-multiline ---
t_set_rejects_blob() {
    # Blob (bare token + KEY= continuation) → rejected, NOT stored.
    if printf 'ghp_AAAA\nEXTRA=other\n' | "$VCT" set --project sg --key blobk 2>"$TMP/sg.err"; then
        echo "    blob was accepted (should reject)"; return 1
    fi
    grep -q "blob_key_eq_continuation" "$TMP/sg.err" || { echo "    reason slug missing"; return 1; }
    [ ! -f "$VCT_SECRETS_DIR/projects/sg/blobk" ] || { echo "    blob file was written"; return 1; }
    # Error must not leak the secret value.
    if grep -q "other" "$TMP/sg.err"; then echo "    error leaked value"; return 1; fi

    # Single-line → accepted.
    printf 'ghp_%s' "$(printf 'a%.0s' $(seq 1 36))" | "$VCT" set --project sg --key clean 2>/dev/null || return 1
    [ -f "$VCT_SECRETS_DIR/projects/sg/clean" ] || return 1

    # PEM → accepted WITHOUT --allow-multiline.
    printf -- '-----BEGIN RSA PRIVATE KEY-----\nMIIE\nAAAA\n-----END RSA PRIVATE KEY-----' \
        | "$VCT" set --project sg --key pemk 2>/dev/null || { echo "    PEM rejected without flag"; return 1; }
    [ -f "$VCT_SECRETS_DIR/projects/sg/pemk" ] || return 1

    # Arbitrary multi-line → accepted WITH --allow-multiline.
    printf 'line1\nline2' | "$VCT" set --project sg --key mlk --allow-multiline 2>/dev/null \
        || { echo "    allow-multiline rejected a multiline value"; return 1; }
    [ -f "$VCT_SECRETS_DIR/projects/sg/mlk" ] || return 1
}

# --- Test: migrate-shared.sh skips a blob source, migrates a clean one ---
t_migrate_shared_blob_skip() {
    local root="$TMP/msstore"
    rm -rf "$root"; mkdir -p "$root"
    # A known-flat clean secret and a known-flat blob (both in KNOWN_FLAT list).
    printf 'ghp_%s' "$(printf 'a%.0s' $(seq 1 36))" > "$root/github_pat"; chmod 600 "$root/github_pat"
    printf 'tok0\nEXTRA=leak-value\n' > "$root/vercel_token"; chmod 600 "$root/vercel_token"
    VCT_SECRETS_DIR="$root" bash "$MIGRATE_SHARED" >"$TMP/ms.out" 2>&1 || return 1
    # Clean one migrated.
    [ -f "$root/shared/github_pat" ] || { echo "    clean not migrated"; return 1; }
    # Blob NOT migrated, left in place, marker dropped.
    [ ! -f "$root/shared/vercel_token" ] || { echo "    blob was migrated"; return 1; }
    [ -f "$root/vercel_token" ] || { echo "    blob source removed"; return 1; }
    [ -f "$root/vercel_token.needs-split" ] || { echo "    .needs-split marker missing"; return 1; }
    grep -q "vercel_token" "$TMP/ms.out" || return 1
    # Output must not leak the blob's embedded value.
    if grep -q "leak-value" "$TMP/ms.out"; then echo "    migrate-shared leaked value"; return 1; fi
}

# --- Test: recover-blob splits, handles collisions, defers, idempotent ---
t_recover_blob() {
    local root="$TMP/rbstore"
    rm -rf "$root"; mkdir -p "$root/projects/rp"
    export VCT_SECRETS_DIR="$root"
    # Standalone keys: same + differ.
    printf 'same-value'  > "$root/projects/rp/COLLIDE_SAME"; chmod 600 "$root/projects/rp/COLLIDE_SAME"
    printf 'standalone-v' > "$root/projects/rp/COLLIDE_DIFF"; chmod 600 "$root/projects/rp/COLLIDE_DIFF"
    # Blob: base token + same-collision + differ-collision + blob-only line.
    printf 'ghp_BASE00000000000000000000000000000000\nCOLLIDE_SAME=same-value\nexport COLLIDE_DIFF=blob-differs\nBLOBONLY=fresh-secret\n' \
        > "$root/projects/rp/github_pat"
    chmod 600 "$root/projects/rp/github_pat"
    # A project folder for the canonical deferral.
    local proj="$TMP/rbproj"
    mkdir -p "$proj/.claude/context"
    printf 'rp\n' > "$proj/.vct-project"

    # Run from the project dir so the deferral lands there. Point vco_lib root
    # at the repo root (HERE/../../..).
    local repo_root
    repo_root=$(cd "$HERE/../../.." && pwd)
    ( cd "$proj" && VCT_INSTALL_ROOT="$repo_root" "$VCT" recover-blob --project rp --key github_pat ) \
        >"$TMP/rb.out" 2>"$TMP/rb.err" || { cat "$TMP/rb.err"; return 1; }

    # Base token rewritten to single-line.
    [ "$(cat "$root/projects/rp/github_pat")" = "ghp_BASE00000000000000000000000000000000" ] \
        || { echo "    base token not rewritten"; return 1; }
    # Blob-only key recovered.
    [ "$(cat "$root/projects/rp/BLOBONLY" 2>/dev/null)" = "fresh-secret" ] \
        || { echo "    blob-only key not recovered"; return 1; }
    # Same-collision: standalone unchanged (dropped extracted).
    [ "$(cat "$root/projects/rp/COLLIDE_SAME")" = "same-value" ] || return 1
    # Differ-collision: standalone KEPT (never overwritten).
    [ "$(cat "$root/projects/rp/COLLIDE_DIFF")" = "standalone-v" ] \
        || { echo "    differ-collision standalone was overwritten"; return 1; }
    # Backup made.
    ls "$root/projects/rp/".github_pat.blob-backup-* >/dev/null 2>&1 \
        || { echo "    no backup made"; return 1; }
    # Audit rows for recover-blob.
    grep -q '"op":"recover-blob"' "$root/audit.log" || { echo "    no recover-blob audit rows"; return 1; }
    # No value leaked to stdout/stderr.
    if grep -qE "same-value|blob-differs|fresh-secret|standalone-v" "$TMP/rb.out" "$TMP/rb.err"; then
        echo "    recover-blob leaked a value"; return 1
    fi
    # Canonical deferral written (only when python3 available).
    if command -v python3 >/dev/null 2>&1; then
        [ -f "$proj/.claude/context/UPDATE_DEFERRED.md" ] || { echo "    deferral not written"; return 1; }
        grep -q "secret_blob_collision" "$proj/.claude/context/UPDATE_DEFERRED.md" || return 1
        grep -q "COLLIDE_DIFF" "$proj/.claude/context/UPDATE_DEFERRED.md" || return 1
        # The deferral must not contain the secret VALUES.
        if grep -qE "blob-differs|standalone-v" "$proj/.claude/context/UPDATE_DEFERRED.md"; then
            echo "    deferral leaked a value"; return 1
        fi
    fi

    # Idempotent re-run: base is now clean → no-op, no new backup.
    local n_before n_after
    n_before=$(ls "$root/projects/rp/".github_pat.blob-backup-* 2>/dev/null | wc -l)
    "$VCT" recover-blob --project rp --key github_pat >"$TMP/rb2.out" 2>&1 || return 1
    grep -q "already single-line-clean" "$TMP/rb2.out" || { echo "    re-run not idempotent"; return 1; }
    n_after=$(ls "$root/projects/rp/".github_pat.blob-backup-* 2>/dev/null | wc -l)
    [ "$n_before" = "$n_after" ] || { echo "    idempotent re-run made a new backup"; return 1; }
}

# --- Test: recover-blob line0-is-KEY= extracts it, leaves original + warns ---
t_recover_blob_line0_key_eq() {
    local root="$TMP/rb0store"
    rm -rf "$root"; mkdir -p "$root/shared"
    export VCT_SECRETS_DIR="$root"
    printf 'INNER=inner-secret\nOTHER=second\n' > "$root/shared/misfiled"; chmod 600 "$root/shared/misfiled"
    "$VCT" recover-blob --shared --key misfiled >"$TMP/rb0.out" 2>"$TMP/rb0.err" || return 1
    [ "$(cat "$root/shared/INNER" 2>/dev/null)" = "inner-secret" ] || { echo "    INNER not extracted"; return 1; }
    [ "$(cat "$root/shared/OTHER" 2>/dev/null)" = "second" ] || return 1
    # Original left in place (backed up).
    [ -f "$root/shared/misfiled" ] || { echo "    original removed"; return 1; }
    grep -q "no bare base token" "$TMP/rb0.err" || { echo "    line0-key= warning missing"; return 1; }
}

# --- Test: doctor taxonomy — blob vs length_corruption vs ok remediation ---
t_doctor_taxonomy() {
    local root="$TMP/dtstore"
    rm -rf "$root"; mkdir -p "$root/shared"
    export VCT_SECRETS_DIR="$root"
    # Blob under github_pat.blob → "recover-blob" remediation.
    printf 'ghp_AAAA\nEXTRA=x\n' > "$root/shared/github_pat.blob"; chmod 600 "$root/shared/github_pat.blob"
    "$VCT" doctor 2>"$TMP/dt.err" || return 1
    grep -q "is a BLOB" "$TMP/dt.err" || { echo "    blob not classified"; return 1; }
    grep -q "recover-blob" "$TMP/dt.err" || { echo "    blob remediation wrong"; return 1; }
    # length_corruption: a ghp_ token that's too long (single line, no newline).
    printf 'ghp_%s' "$(printf 'a%.0s' $(seq 1 60))" > "$root/shared/github_pat.long"; chmod 600 "$root/shared/github_pat.long"
    "$VCT" doctor 2>"$TMP/dt2.err" || return 1
    grep -q "LENGTH-CORRUPTION" "$TMP/dt2.err" || { echo "    length_corruption not classified"; return 1; }
    # doctor without --fix-shape must NOT mutate the blob (still multi-line).
    grep -q "EXTRA=x" "$root/shared/github_pat.blob" || { echo "    doctor mutated a secret without --fix-shape"; return 1; }
    # Values never leaked.
    if grep -q "EXTRA=x" "$TMP/dt.err" "$TMP/dt2.err"; then echo "    doctor leaked value"; return 1; fi
}

# --- Test: doctor --fix-shape recovers a blob github_pat file ---
t_doctor_fix_shape() {
    local root="$TMP/dfstore"
    rm -rf "$root"; mkdir -p "$root/shared"
    export VCT_SECRETS_DIR="$root"
    printf 'ghp_BASE00000000000000000000000000000000\nSIDEKEY=side-secret\n' > "$root/shared/github_pat"
    chmod 600 "$root/shared/github_pat"
    "$VCT" doctor --fix-shape >"$TMP/df.out" 2>"$TMP/df.err" || return 1
    # Base rewritten to the single-line token.
    [ "$(cat "$root/shared/github_pat")" = "ghp_BASE00000000000000000000000000000000" ] \
        || { echo "    --fix-shape did not recover the blob"; return 1; }
    # Side key extracted.
    [ "$(cat "$root/shared/SIDEKEY" 2>/dev/null)" = "side-secret" ] || return 1
    grep -q "blob(s) recovered" "$TMP/df.err" || return 1
    if grep -q "side-secret" "$TMP/df.out" "$TMP/df.err"; then echo "    --fix-shape leaked value"; return 1; fi
}

# --- Run ---
printf 'Running vct test suite (VCT_SECRETS_DIR=%s)\n\n' "$VCT_SECRETS_DIR"

run_test "help"                        t_help
run_test "set refuses --value on argv" t_set_refuses_value_argv
run_test "set reads stdin, chmod 600"  t_set_stdin
run_test "list shows keys, no values"  t_list
run_test "exec injects env into child" t_exec_injects
run_test "exec KEY=VAR renaming"       t_exec_rename
run_test "exec multiple secrets"       t_exec_multiple
run_test "resolution: project > shared" t_resolution_order
run_test "exec missing → exit 2, no run" t_exec_missing_fails_fast
run_test "revoke removes file"         t_revoke
run_test "path injection rejected"     t_path_injection
run_test "migrate-from-env parses .env" t_migrate_from_env
run_test "migrate-from-env --dry-run"  t_migrate_dry_run
run_test "audit log written, no values" t_audit_log
run_test "doctor fixes chmod"          t_doctor
run_test "detect-project walks up"     t_detect_project
run_test "get works in non-TTY"        t_get_non_tty
run_test "copy between projects"       t_copy
run_test "get/exec default to shared (S-2)" t_shared_default_get_exec
run_test "can-read exit codes (S-5)"   t_can_read
run_test "resolve prints source path (S-5)" t_resolve_path
run_test "doctor github_pat shape check (S-3)" t_doctor_token_shape
run_test "die_miss 403 withholds 'vct set' (4b)" t_die_miss_403_withholds_vct_set
run_test "die_miss hit points at resolver (4b)"  t_die_miss_hit_points_at_resolver

# v0.2.80 Part A/B/C — secret-shape guard + recover-blob + doctor taxonomy.
# These run LAST: several set their own isolated VCT_SECRETS_DIR.
run_test "shape predicate parity fixture (bash leg)"  t_shape_parity_fixture
run_test "set rejects blob / accepts PEM+allow-ml"    t_set_rejects_blob
run_test "migrate-shared skips blob + .needs-split"   t_migrate_shared_blob_skip
run_test "recover-blob split/collision/defer/idemp"   t_recover_blob
run_test "recover-blob line0-is-KEY= handled"         t_recover_blob_line0_key_eq
run_test "doctor taxonomy (blob/length/ok)"           t_doctor_taxonomy
run_test "doctor --fix-shape recovers blob"           t_doctor_fix_shape

printf '\n=== Results: %d passed, %d failed ===\n' "$PASS" "$FAIL"
if [ $FAIL -gt 0 ]; then
    printf 'Failed tests:\n'
    printf '  - %s\n' "${FAILED_TESTS[@]}"
    exit 1
fi
exit 0
