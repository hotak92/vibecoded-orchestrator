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

printf '\n=== Results: %d passed, %d failed ===\n' "$PASS" "$FAIL"
if [ $FAIL -gt 0 ]; then
    printf 'Failed tests:\n'
    printf '  - %s\n' "${FAILED_TESTS[@]}"
    exit 1
fi
exit 0
