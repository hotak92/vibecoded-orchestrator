#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Pure-Bash test suite for the two `vct` gaps closed alongside the v0.2.80
# value-shape guard. Same harness conventions as tools/vct-secrets/tests/
# test_vct.sh (no bats, VCT_SECRETS_DIR override, ok/ko counters) — a separate
# FILE only so the existing suite stays untouched.
#
#   GAP 1 (security)     — validate_name accepted a KEY/PROJECT name that IS a
#                          live credential, so a swapped argument
#                          (`vct set --key <the token>`) turned the credential
#                          into a FILENAME and a cleartext audit.log row. The
#                          rejection message also echoed the rejected input,
#                          which made the check that exists to prevent an
#                          exposure into a second exposure.
#   GAP 2 (availability) — `set` hardcoded projects/<project>/<key>, so
#                          `--project shared` created a project literally named
#                          "shared" that no shared-namespace reader consults.
#
# Plus the delivery-mechanism legs: symlink self-resolution (the way to deploy
# the CLI without taking a copy that rots) and the doctor detectors for an
# already-damaged store.
#
# ── FIXTURES ARE SYNTHETIC AND MUST STAY THAT WAY ──────────────────────────
# Every credential-shaped string below is a visibly fake repeating/constant
# pattern (or the AWS documentation example key, which tests/
# test_v52_l1_subagent_stop_reconciler.py already uses for this purpose).
# NEVER put a plausible-looking random token in this file: it would be
# indistinguishable from a real leak to every scanner that reads the repo,
# starting with scripts/check-no-secrets.sh.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
# VCT_UNDER_TEST lets the red-proof run point this suite at a pre-fix build.
VCT="${VCT_UNDER_TEST:-$HERE/../vct}"

if [ ! -x "$VCT" ]; then
    printf 'FAIL: vct not executable at %s\n' "$VCT" >&2
    exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export VCT_SECRETS_DIR="$TMP/store"
# Hermeticity (mirrors tests/test_vct_secrets_cli_suite_runner.py): point the
# hub discovery at an empty dir and strip any inherited token/port so the
# miss-path probe soft-fails identically on every machine.
export VCT_STATE_DIR="$TMP/empty-state"
mkdir -p "$VCT_STATE_DIR"
unset VCT_HUB_TOKEN VCT_HUB_PORT VCT_HUB_TOKEN_STRICT 2>/dev/null || true

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

# ---------- Synthetic credential-shaped fixtures ----------
# Visibly fake: constant runs and the repeated word "deadbeef". The
# exact-length shapes are BUILT from repeats rather than typed as literals —
# partly so a miscount cannot silently weaken a test, partly so no literal
# 40/93-char token-shaped string exists anywhere in this repo for
# scripts/check-no-secrets.sh to have to reason about.
_rep() { local c=$1 n=$2; printf "$c%.0s" $(seq 1 "$n"); }

FX_OPENROUTER="sk-or-v1-$(_rep deadbeef 5)"   # built, not typed: canonical 64-hex literals trip GitHub push protection
FX_ANTHROPIC="sk-ant-api03-$(_rep A 30)"
FX_GH_CLASSIC="ghp_$(_rep A 36)"                               # classic PAT: prefix + 36
FX_GH_FINE="github_pat_$(_rep A 22)_$(_rep B 59)"              # fine-grained: 11+22+1+59 = 93
# AWS's own documentation example key. tests/test_v52_l1_subagent_stop_
# reconciler.py already uses this exact literal for the same reason: it has
# the shape and is unmistakably not a real credential.
FX_AWS='AKIAIOSFODNN7EXAMPLE'
FX_JWT='eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0'                   # {"alg":"none","typ":"JWT"}
FX_GITLAB="glpat-$(_rep A 20)"
FX_SLACK="xoxb-$(_rep A 20)"

# Ordinary names that MUST keep working (the leave-alone side of the decision).
OK_NAMES=(
    API_KEY
    github_pat
    github_pat.myorg
    github_pat_personal
    openai_api_key
    sk_test_key
    my-service-token
    supabase_token
)

# ---------- Helpers ----------

# assert_nothing_written NAME — no file anywhere under the store is named NAME
# and no path contains it.
assert_nothing_written() {
    local name=$1 hit
    hit=$(find "$VCT_SECRETS_DIR" -name "*$name*" 2>/dev/null | head -n1)
    if [ -n "$hit" ]; then
        printf '    a path bearing the rejected name was created\n' >&2
        return 1
    fi
    return 0
}

# assert_not_audited NAME — audit.log has no row mentioning NAME.
assert_not_audited() {
    local name=$1 log="$VCT_SECRETS_DIR/audit.log"
    [ -f "$log" ] || return 0
    if grep -qF -- "$name" "$log"; then
        printf '    the rejected name reached audit.log\n' >&2
        return 1
    fi
    return 0
}

# assert_not_echoed FILE NAME — FILE (captured stderr/stdout) does not contain
# NAME. THIS IS THE FIX for the secondary leak, so it is asserted explicitly
# everywhere a name is rejected.
assert_not_echoed() {
    local f=$1 name=$2
    if grep -qF -- "$name" "$f"; then
        printf '    the rejected name was echoed back in the output\n' >&2
        return 1
    fi
    return 0
}

# ---------- GAP 1: credential-shaped NAME is refused ----------

# Every known shape is refused at `set`, nothing lands, nothing is logged, and
# the message never quotes the input.
t_set_rejects_credential_shaped_key() {
    local fx
    for fx in "$FX_OPENROUTER" "$FX_ANTHROPIC" "$FX_GH_CLASSIC" "$FX_GH_FINE" \
              "$FX_AWS" "$FX_JWT" "$FX_GITLAB" "$FX_SLACK"; do
        rm -rf "$VCT_SECRETS_DIR"
        if printf 'x' | "$VCT" set --project demo --key "$fx" >"$TMP/o" 2>"$TMP/e"; then
            printf '    ACCEPTED a credential-shaped key name\n' >&2
            return 1
        fi
        assert_nothing_written "$fx" || return 1
        assert_not_audited "$fx"     || return 1
        assert_not_echoed "$TMP/e" "$fx" || return 1
        assert_not_echoed "$TMP/o" "$fx" || return 1
    done
}

# The same guard on the PROJECT position (the other half of a swapped argument).
t_set_rejects_credential_shaped_project() {
    rm -rf "$VCT_SECRETS_DIR"
    if printf 'x' | "$VCT" set --project "$FX_OPENROUTER" --key API_KEY >"$TMP/o" 2>"$TMP/e"; then
        printf '    ACCEPTED a credential-shaped project name\n' >&2
        return 1
    fi
    assert_nothing_written "$FX_OPENROUTER" || return 1
    assert_not_audited "$FX_OPENROUTER"     || return 1
    assert_not_echoed "$TMP/e" "$FX_OPENROUTER" || return 1
}

# ORDERING PROOF: the guard fires before the audit write AND before any file is
# created. Pre-seed a store that already has an audit.log with a known row, then
# assert the rejected run appended nothing at all — a guard that runs after
# `audit` would have added a line even though no secret file survived.
t_guard_precedes_audit_and_file() {
    rm -rf "$VCT_SECRETS_DIR"
    printf 'seed' | "$VCT" set --project demo --key SEED >/dev/null 2>&1 || return 1
    local log="$VCT_SECRETS_DIR/audit.log"
    local before after
    before=$(wc -l < "$log")
    if printf 'x' | "$VCT" set --project demo --key "$FX_OPENROUTER" >/dev/null 2>&1; then
        printf '    guard did not fire\n' >&2; return 1
    fi
    after=$(wc -l < "$log")
    [ "$before" = "$after" ] || { printf '    audit.log grew on a rejected set\n' >&2; return 1; }
    assert_nothing_written "$FX_OPENROUTER" || return 1
    # And no leftover mktemp scratch file either.
    [ -z "$(find "$VCT_SECRETS_DIR" -name '.*tmp*' 2>/dev/null | head -n1)" ] \
        || { printf '    a temp file survived the rejection\n' >&2; return 1; }
}

# Leave-alone: ordinary names are untouched by the guard.
t_ordinary_names_still_accepted() {
    rm -rf "$VCT_SECRETS_DIR"
    local n
    for n in "${OK_NAMES[@]}"; do
        printf 'val-%s' "$n" | "$VCT" set --project demo --key "$n" 2>"$TMP/e" \
            || { printf '    REJECTED an ordinary key name: %s\n' "$n" >&2; return 1; }
        [ -f "$VCT_SECRETS_DIR/projects/demo/$n" ] \
            || { printf '    not stored: %s\n' "$n" >&2; return 1; }
    done
    # And a project name that merely CONTAINS a prefix is fine.
    printf 'v' | "$VCT" set --project sk-app --key API_KEY 2>/dev/null \
        || { printf '    REJECTED an ordinary project name\n' >&2; return 1; }
}

# The pre-existing structural rejections must ALSO stop echoing the input.
# This is the arm a pasted PEM header or a path-traversal attempt hits.
t_structural_rejections_do_not_echo() {
    rm -rf "$VCT_SECRETS_DIR"
    local bad='../ESCAPE-PROBE-VALUE'
    if printf 'x' | "$VCT" set --project demo --key "$bad" >"$TMP/o" 2>"$TMP/e"; then
        return 1
    fi
    assert_not_echoed "$TMP/e" "ESCAPE-PROBE-VALUE" || return 1
    # Over-length arm.
    local long
    long=$(printf 'L%.0s' $(seq 1 200))
    if printf 'x' | "$VCT" set --project demo --key "$long" >"$TMP/o" 2>"$TMP/e2"; then
        return 1
    fi
    assert_not_echoed "$TMP/e2" "$long" || return 1
    # Out-of-charset arm.
    if printf 'x' | "$VCT" set --project demo --key 'BAD KEY$PROBE' >"$TMP/o" 2>"$TMP/e3"; then
        return 1
    fi
    assert_not_echoed "$TMP/e3" 'BAD KEY$PROBE' || return 1
}

# copy / migrate-from-env / recover-blob are CREATE boundaries too.
t_other_create_boundaries_guarded() {
    rm -rf "$VCT_SECRETS_DIR"
    printf 'v' | "$VCT" set --project src --key API_KEY >/dev/null 2>&1 || return 1
    # copy: guarded on key and on both project positions.
    if "$VCT" copy --from-project src --to-project "$FX_GH_CLASSIC" --key API_KEY --yes \
        >"$TMP/o" 2>"$TMP/e"; then
        printf '    copy accepted a credential-shaped destination project\n' >&2; return 1
    fi
    assert_not_echoed "$TMP/e" "$FX_GH_CLASSIC" || return 1
    assert_nothing_written "$FX_GH_CLASSIC" || return 1

    # migrate-from-env: SKIPS the bad line (never aborts the whole file) and
    # never names it.
    local envf="$TMP/probe.env"
    {
        printf 'GOOD_ONE=value1\n'
        printf '%s=value2\n' "$FX_OPENROUTER"
        printf 'GOOD_TWO=value3\n'
    } > "$envf"
    "$VCT" migrate-from-env "$envf" --project mig >"$TMP/mo" 2>"$TMP/me" || return 1
    [ -f "$VCT_SECRETS_DIR/projects/mig/GOOD_ONE" ] || { printf '    good key not imported\n' >&2; return 1; }
    [ -f "$VCT_SECRETS_DIR/projects/mig/GOOD_TWO" ] || { printf '    migration aborted early\n' >&2; return 1; }
    assert_nothing_written "$FX_OPENROUTER" || return 1
    assert_not_echoed "$TMP/mo" "$FX_OPENROUTER" || return 1
    assert_not_echoed "$TMP/me" "$FX_OPENROUTER" || return 1
    assert_not_audited "$FX_OPENROUTER" || return 1

    # recover-blob: an extracted "KEY" that is credential-shaped is skipped.
    local root="$TMP/rbstore"; rm -rf "$root"; mkdir -p "$root/shared"
    VCT_SECRETS_DIR="$root" "$VCT" version >/dev/null 2>&1
    printf 'ghp_BASE00000000000000000000000000000000\n%s=x\nSAFE=keepme\n' "$FX_OPENROUTER" \
        > "$root/shared/github_pat"
    chmod 600 "$root/shared/github_pat"
    VCT_SECRETS_DIR="$root" "$VCT" recover-blob --shared --key github_pat >"$TMP/ro" 2>"$TMP/re" || return 1
    [ -f "$root/shared/SAFE" ] || { printf '    safe extracted key missing\n' >&2; return 1; }
    [ -e "$root/shared/$FX_OPENROUTER" ] \
        && { printf '    recover-blob minted a credential-shaped filename\n' >&2; return 1; }
    assert_not_echoed "$TMP/ro" "$FX_OPENROUTER" || return 1
    assert_not_echoed "$TMP/re" "$FX_OPENROUTER" || return 1
    return 0
}

# The READ twin: `vct get --key <token>` must not hand the name to the hub
# probe (a URL query string → curl → the hub's request log) or echo it.
t_read_path_does_not_leak_credential_shaped_name() {
    rm -rf "$VCT_SECRETS_DIR"
    "$VCT" version >/dev/null 2>&1
    if "$VCT" get --project demo --key "$FX_OPENROUTER" >"$TMP/o" 2>"$TMP/e"; then
        printf '    get succeeded on a nonexistent key\n' >&2; return 1
    fi
    assert_not_echoed "$TMP/e" "$FX_OPENROUTER" || return 1
    assert_not_audited "$FX_OPENROUTER" || return 1
}

# Remediation must stay OPEN: a store damaged before the guard existed must
# still be cleanable with the tool, and the cleanup must not reprint the name.
t_revoke_remains_available_for_damaged_store() {
    local root="$TMP/dmg"; rm -rf "$root"; mkdir -p "$root/projects/demo"
    printf 'whatever' > "$root/projects/demo/$FX_OPENROUTER"
    chmod 600 "$root/projects/demo/$FX_OPENROUTER"
    VCT_SECRETS_DIR="$root" "$VCT" revoke --project demo --key "$FX_OPENROUTER" --yes \
        >"$TMP/o" 2>"$TMP/e" \
        || { printf '    revoke refused to clean up a damaged store\n' >&2; return 1; }
    [ -e "$root/projects/demo/$FX_OPENROUTER" ] \
        && { printf '    revoke did not remove the file\n' >&2; return 1; }
    assert_not_echoed "$TMP/e" "$FX_OPENROUTER" || return 1
    assert_not_echoed "$TMP/o" "$FX_OPENROUTER" || return 1
    # The audit row for the revoke must be redacted, not verbatim.
    if grep -qF -- "$FX_OPENROUTER" "$root/audit.log" 2>/dev/null; then
        printf '    revoke wrote the credential-shaped name into audit.log\n' >&2; return 1
    fi
    grep -q '"op":"revoke"' "$root/audit.log" || { printf '    revoke not audited at all\n' >&2; return 1; }
    return 0
}

# ---------- GAP 2: the shared-scope write path ----------

# --shared lands where every shared-namespace READER actually looks.
t_set_shared_lands_in_shared_namespace() {
    rm -rf "$VCT_SECRETS_DIR"
    printf 'shared-val' | "$VCT" set --shared --key SHARED_KEY 2>"$TMP/e" || return 1
    [ -f "$VCT_SECRETS_DIR/shared/SHARED_KEY" ] \
        || { printf '    --shared did not write shared/\n' >&2; return 1; }
    [ -e "$VCT_SECRETS_DIR/projects/shared/SHARED_KEY" ] \
        && { printf '    --shared wrote projects/shared/ too\n' >&2; return 1; }
    [ "$(stat -c '%a' "$VCT_SECRETS_DIR/shared/SHARED_KEY")" = "600" ] || return 1
    # The success message names the real path (the old one said "shared/K" for
    # a write that had landed elsewhere).
    grep -qF -- "$VCT_SECRETS_DIR/shared/SHARED_KEY" "$TMP/e" \
        || { printf '    success message does not name the resolved path\n' >&2; return 1; }
    # A per-project reader — the population that could never see the old write
    # — resolves it now.
    local got
    got=$("$VCT" get --project someotherproject --key SHARED_KEY 2>/dev/null)
    [ "$got" = "shared-val" ] || { printf '    per-project read cannot see it\n' >&2; return 1; }
    return 0
}

# `--project shared` is aliased (not refused) and lands in the same place.
t_project_shared_is_aliased_to_shared() {
    rm -rf "$VCT_SECRETS_DIR"
    printf 'aliased' | "$VCT" set --project shared --key ALIAS_KEY 2>"$TMP/e" || return 1
    [ -f "$VCT_SECRETS_DIR/shared/ALIAS_KEY" ] \
        || { printf '    --project shared still misfiles\n' >&2; return 1; }
    [ -e "$VCT_SECRETS_DIR/projects/shared/ALIAS_KEY" ] \
        && { printf '    --project shared still created projects/shared/\n' >&2; return 1; }
    grep -q -- "--shared" "$TMP/e" \
        || { printf '    no note pointing at the canonical spelling\n' >&2; return 1; }
    # Readers see it.
    [ "$("$VCT" get --project anyproj --key ALIAS_KEY 2>/dev/null)" = "aliased" ] || return 1
    return 0
}

# Leave-alone (PURE): an ordinary project is completely unaffected by the scope
# work. This one passes BOTH before and after the fix, by design — it is the
# "did you break the thing that already worked" half of the decision.
t_ordinary_project_unaffected() {
    rm -rf "$VCT_SECRETS_DIR"
    printf 'p-val' | "$VCT" set --project myproj --key PKEY 2>/dev/null || return 1
    [ -f "$VCT_SECRETS_DIR/projects/myproj/PKEY" ] \
        || { printf '    ordinary project write moved\n' >&2; return 1; }
    [ -e "$VCT_SECRETS_DIR/shared/PKEY" ] \
        && { printf '    ordinary project write leaked into shared/\n' >&2; return 1; }
    [ "$("$VCT" get --project myproj --key PKEY 2>/dev/null)" = "p-val" ] || return 1
    [ -e "$VCT_SECRETS_DIR/projects/shared" ] \
        && { printf '    an ordinary write created projects/shared/\n' >&2; return 1; }
    return 0
}

# Resolution precedence is unchanged now that both scopes are writable:
# projects/<NAME>/ still wins over shared/ for the same key.
t_project_scope_still_wins_over_shared() {
    rm -rf "$VCT_SECRETS_DIR"
    printf 'p-val' | "$VCT" set --project myproj --key DUPKEY 2>/dev/null || return 1
    printf 's-val' | "$VCT" set --shared --key DUPKEY 2>/dev/null || return 1
    [ "$("$VCT" get --project myproj --key DUPKEY 2>/dev/null)" = "p-val" ] \
        || { printf '    project scope no longer wins\n' >&2; return 1; }
    # A project with no override falls through to the shared copy.
    [ "$("$VCT" get --project otherproj --key DUPKEY 2>/dev/null)" = "s-val" ] \
        || { printf '    fallthrough to shared broken\n' >&2; return 1; }
    return 0
}

# --shared and --project NAME cannot both be given.
t_shared_and_project_are_exclusive() {
    rm -rf "$VCT_SECRETS_DIR"
    if printf 'x' | "$VCT" set --shared --project myproj --key K 2>"$TMP/e"; then
        return 1
    fi
    grep -q "mutually exclusive" "$TMP/e" || return 1
}

# `revoke` speaks the same scope vocabulary — otherwise `--project shared`
# would mean the shared namespace in `set` and projects/shared/ in `revoke`.
t_revoke_shared_scope_symmetry() {
    rm -rf "$VCT_SECRETS_DIR"
    printf 'v' | "$VCT" set --shared --key RKEY 2>/dev/null || return 1
    "$VCT" revoke --shared --key RKEY --yes 2>/dev/null \
        || { printf '    revoke --shared failed\n' >&2; return 1; }
    [ -e "$VCT_SECRETS_DIR/shared/RKEY" ] && { printf '    not removed\n' >&2; return 1; }
    # And the alias spelling reaches the same file.
    printf 'v' | "$VCT" set --shared --key RKEY2 2>/dev/null || return 1
    "$VCT" revoke --project shared --key RKEY2 --yes 2>/dev/null || return 1
    [ -e "$VCT_SECRETS_DIR/shared/RKEY2" ] && return 1
    return 0
}

# doctor must stop recommending the invocation that silently fails, and must
# emit a scope flag that is actually pasteable for a PROJECT-scoped file.
t_doctor_remediation_is_pasteable() {
    local root="$TMP/docrem"; rm -rf "$root"; mkdir -p "$root/projects/proj1"
    # A single-line malformed ghp_ token → the length_corruption arm, which
    # used to emit `vct set --key KEY` with no scope at all.
    printf 'ghp_%s' "$(printf 'a%.0s' $(seq 1 60))" > "$root/projects/proj1/github_pat"
    chmod 600 "$root/projects/proj1/github_pat"
    VCT_SECRETS_DIR="$root" "$VCT" doctor 2>"$TMP/dr.err" || return 1
    grep -q "LENGTH-CORRUPTION" "$TMP/dr.err" || return 1
    grep -q -- "vct set --project proj1 --key github_pat" "$TMP/dr.err" \
        || { printf '    remediation lacks a usable scope flag\n' >&2; return 1; }
    if grep -q -- "vct set --project shared" "$TMP/dr.err"; then
        printf '    doctor still recommends the misfiling invocation\n' >&2; return 1
    fi
    return 0
}

# ---------- GAP 2, already-damaged axis ----------

# Default doctor DETECTS projects/shared/ and does not touch it.
t_doctor_detects_misfiled_shared_without_mutating() {
    local root="$TMP/misf"; rm -rf "$root"; mkdir -p "$root/projects/shared"
    printf 'orphan' > "$root/projects/shared/ORPHAN"; chmod 600 "$root/projects/shared/ORPHAN"
    VCT_SECRETS_DIR="$root" "$VCT" doctor 2>"$TMP/mf.err" || return 1
    grep -q "misfiled" "$TMP/mf.err" || { printf '    not detected\n' >&2; return 1; }
    grep -q -- "--fix-shared-scope" "$TMP/mf.err" || { printf '    no remediation offered\n' >&2; return 1; }
    # NOT moved — doctor without an explicit --fix-* flag never mutates a secret.
    [ -f "$root/projects/shared/ORPHAN" ] \
        || { printf '    doctor moved a secret without consent\n' >&2; return 1; }
    [ -e "$root/shared/ORPHAN" ] && { printf '    doctor auto-migrated\n' >&2; return 1; }
    # The value is never printed.
    if grep -q "orphan" "$TMP/mf.err"; then printf '    value leaked\n' >&2; return 1; fi
    return 0
}

# Opt-in move: atomic, audited, and the value ends up in exactly one place.
t_doctor_fix_shared_scope_moves_atomically() {
    local root="$TMP/misf2"; rm -rf "$root"; mkdir -p "$root/projects/shared"
    printf 'orphan-value' > "$root/projects/shared/ORPHAN"; chmod 600 "$root/projects/shared/ORPHAN"
    VCT_SECRETS_DIR="$root" "$VCT" doctor --fix-shared-scope 2>"$TMP/mf2.err" || return 1
    [ -f "$root/shared/ORPHAN" ] || { printf '    not moved\n' >&2; return 1; }
    [ "$(cat "$root/shared/ORPHAN")" = "orphan-value" ] || { printf '    value changed\n' >&2; return 1; }
    [ -e "$root/projects/shared/ORPHAN" ] && { printf '    left a second copy behind\n' >&2; return 1; }
    [ "$(stat -c '%a' "$root/shared/ORPHAN")" = "600" ] || return 1
    grep -q '"op":"promote-shared"' "$root/audit.log" || { printf '    move not audited\n' >&2; return 1; }
    if grep -q "orphan-value" "$TMP/mf2.err" "$root/audit.log"; then
        printf '    value leaked\n' >&2; return 1
    fi
    return 0
}

# Collision: never overwrite, never delete, report both. This is the branch
# where "migrate automatically" would have destroyed data.
t_doctor_fix_shared_scope_refuses_collision() {
    local root="$TMP/misf3"; rm -rf "$root"; mkdir -p "$root/projects/shared" "$root/shared"
    printf 'from-projects' > "$root/projects/shared/DUP"; chmod 600 "$root/projects/shared/DUP"
    printf 'from-shared'   > "$root/shared/DUP";          chmod 600 "$root/shared/DUP"
    VCT_SECRETS_DIR="$root" "$VCT" doctor --fix-shared-scope 2>"$TMP/mf3.err" || return 1
    # BOTH survive, untouched.
    [ "$(cat "$root/shared/DUP")" = "from-shared" ] \
        || { printf '    OVERWROTE the existing shared secret\n' >&2; return 1; }
    [ "$(cat "$root/projects/shared/DUP")" = "from-projects" ] \
        || { printf '    destroyed the misfiled copy\n' >&2; return 1; }
    grep -q "refusing to overwrite" "$TMP/mf3.err" || { printf '    collision not reported\n' >&2; return 1; }
    if grep -qE "from-shared|from-projects" "$TMP/mf3.err"; then
        printf '    value leaked\n' >&2; return 1
    fi
    return 0
}

# Leave-alone: a store with no projects/shared/ is silent about it.
t_doctor_silent_without_misfiled_shared() {
    local root="$TMP/clean"; rm -rf "$root"; mkdir -p "$root/projects/normal"
    printf 'v' > "$root/projects/normal/K"; chmod 600 "$root/projects/normal/K"
    VCT_SECRETS_DIR="$root" "$VCT" doctor 2>"$TMP/cl.err" || return 1
    if grep -q "shared-scope" "$TMP/cl.err"; then
        printf '    false positive on a clean store\n' >&2; return 1
    fi
    return 0
}

# Credential-shaped filenames already on disk are DETECTED (count + directory),
# never re-printed.
t_doctor_detects_credential_shaped_filenames() {
    local root="$TMP/shaped"; rm -rf "$root"; mkdir -p "$root/projects/demo"
    printf 'x' > "$root/projects/demo/$FX_OPENROUTER"; chmod 600 "$root/projects/demo/$FX_OPENROUTER"
    VCT_SECRETS_DIR="$root" "$VCT" doctor 2>"$TMP/sh.err" || return 1
    grep -q "name-shape" "$TMP/sh.err" || { printf '    not detected\n' >&2; return 1; }
    grep -q -i "rotate" "$TMP/sh.err" || { printf '    no rotation advice\n' >&2; return 1; }
    assert_not_echoed "$TMP/sh.err" "$FX_OPENROUTER" || return 1
    return 0
}

# ---------- Delivery: symlink self-resolution ----------

# The documented "put it on your PATH" deployment is a symlink. Before the
# self-resolution fix, `dirname $BASH_SOURCE` yielded the SYMLINK's directory,
# so the CLI could not find lib/secret_shape.sh and hard-exited "broken
# install" — i.e. the one delivery mechanism that cannot go stale was the one
# that could not start.
t_runs_through_a_symlink() {
    local bin="$TMP/symbin"; rm -rf "$bin"; mkdir -p "$bin"
    ln -s "$(cd "$(dirname "$VCT")" && pwd)/$(basename "$VCT")" "$bin/vct"
    local root="$TMP/symstore"; rm -rf "$root"
    VCT_SECRETS_DIR="$root" "$bin/vct" version >"$TMP/sv.out" 2>"$TMP/sv.err" \
        || { printf '    symlinked invocation failed: %s\n' "$(cat "$TMP/sv.err")" >&2; return 1; }
    grep -q '^vct ' "$TMP/sv.out" || return 1
    # And a real write works through the symlink (the predicate lib loaded).
    printf 'v' | VCT_SECRETS_DIR="$root" "$bin/vct" set --shared --key SYMKEY 2>/dev/null || return 1
    [ -f "$root/shared/SYMKEY" ] || return 1
    return 0
}

# The capability stamp is what makes a stale deployed COPY detectable.
t_version_reports_guard_capabilities() {
    "$VCT" version > "$TMP/ver.out" 2>&1 || return 1
    grep -q '^guards: ' "$TMP/ver.out" || { printf '    no guards line\n' >&2; return 1; }
    local g
    for g in value-shape name-shape shared-scope symlink-self; do
        grep -q -- "$g" "$TMP/ver.out" || { printf '    missing capability: %s\n' "$g" >&2; return 1; }
    done
}

# A second, guard-less copy inside the store is reported and NEVER overwritten.
t_doctor_detects_stale_deployed_copy() {
    local root="$TMP/deploy"; rm -rf "$root"; mkdir -p "$root"
    # A stand-in for an old deployed copy: a script with none of the guards.
    printf '#!/usr/bin/env bash\necho old\n' > "$root/vct"
    chmod 700 "$root/vct"
    local before
    before=$(cat "$root/vct")
    VCT_SECRETS_DIR="$root" "$VCT" doctor 2>"$TMP/dp.err" || return 1
    grep -q "deployed-copy" "$TMP/dp.err" || { printf '    not detected\n' >&2; return 1; }
    grep -q "capability stamp" "$TMP/dp.err" || { printf '    guard diff not reported\n' >&2; return 1; }
    grep -q "ln -sfn" "$TMP/dp.err" || { printf '    no refresh command offered\n' >&2; return 1; }
    [ "$(cat "$root/vct")" = "$before" ] \
        || { printf '    doctor OVERWROTE the deployed copy\n' >&2; return 1; }
    return 0
}

# Leave-alone: no second copy in the store → nothing said about deployment.
t_doctor_silent_without_deployed_copy() {
    local root="$TMP/nodeploy"; rm -rf "$root"; mkdir -p "$root/shared"
    VCT_SECRETS_DIR="$root" "$VCT" doctor 2>"$TMP/nd.err" || return 1
    if grep -q "deployed-copy" "$TMP/nd.err"; then
        printf '    false positive\n' >&2; return 1
    fi
    return 0
}

# Leave-alone: the store copy IS the running script (the legacy deployment
# where ~/.vct-secrets is on PATH). It must not report itself as a stale
# second copy.
t_doctor_silent_when_running_from_the_store() {
    local root="$TMP/selfdeploy"; rm -rf "$root"; mkdir -p "$root/lib" "$root/shared"
    local srcdir; srcdir=$(cd "$(dirname "$VCT")" && pwd)
    cp "$VCT" "$root/vct"
    # Copy the WHOLE lib/ dir, not one named file: `vct` hard-fails on ANY
    # missing shared predicate, so enumerating libs here makes this fixture
    # break every time a new one is added (it did, when lib/credential_shapes.sh
    # landed). The test is about doctor's self-recognition, not about lib
    # inventory — give it a complete deployment and it stays true.
    cp "$srcdir/lib/"*.sh "$root/lib/" 2>/dev/null \
        || cp "$srcdir/../lib/"*.sh "$root/lib/"
    chmod 700 "$root/vct"
    VCT_SECRETS_DIR="$root" "$root/vct" doctor 2>"$TMP/sd.err" || return 1
    if grep -q "deployed-copy" "$TMP/sd.err"; then
        printf '    reported itself as a stale second copy\n' >&2; return 1
    fi
    return 0
}

# ---------- Run ----------
printf 'Running vct name-shape + shared-scope suite (VCT=%s)\n\n' "$VCT"

run_test "GAP1 set rejects credential-shaped key"       t_set_rejects_credential_shaped_key
run_test "GAP1 set rejects credential-shaped project"   t_set_rejects_credential_shaped_project
run_test "GAP1 guard precedes audit AND file creation"  t_guard_precedes_audit_and_file
run_test "GAP1 ordinary names still accepted"           t_ordinary_names_still_accepted
run_test "GAP1 structural rejections do not echo input" t_structural_rejections_do_not_echo
run_test "GAP1 copy/migrate/recover-blob guarded"       t_other_create_boundaries_guarded
run_test "GAP1 read path does not leak the name"        t_read_path_does_not_leak_credential_shaped_name
run_test "GAP1 revoke stays open for a damaged store"   t_revoke_remains_available_for_damaged_store
run_test "GAP2 --shared lands where readers look"       t_set_shared_lands_in_shared_namespace
run_test "GAP2 --project shared aliased, not misfiled"  t_project_shared_is_aliased_to_shared
run_test "GAP2 ordinary project unaffected"             t_ordinary_project_unaffected
run_test "GAP2 project scope still wins over shared"    t_project_scope_still_wins_over_shared
run_test "GAP2 --shared/--project mutually exclusive"   t_shared_and_project_are_exclusive
run_test "GAP2 revoke shares the scope vocabulary"      t_revoke_shared_scope_symmetry
run_test "GAP2 doctor remediation is pasteable"         t_doctor_remediation_is_pasteable
run_test "GAP2 damaged: detect, do not mutate"          t_doctor_detects_misfiled_shared_without_mutating
run_test "GAP2 damaged: opt-in move is atomic"          t_doctor_fix_shared_scope_moves_atomically
run_test "GAP2 damaged: collision refuses to overwrite" t_doctor_fix_shared_scope_refuses_collision
run_test "GAP2 damaged: silent on a clean store"        t_doctor_silent_without_misfiled_shared
run_test "GAP1 damaged: shaped filenames detected"      t_doctor_detects_credential_shaped_filenames
run_test "DELIVERY runs through a symlink"              t_runs_through_a_symlink
run_test "DELIVERY version reports capabilities"        t_version_reports_guard_capabilities
run_test "DELIVERY stale deployed copy detected"        t_doctor_detects_stale_deployed_copy
run_test "DELIVERY silent without a deployed copy"      t_doctor_silent_without_deployed_copy
run_test "DELIVERY silent when run from the store"      t_doctor_silent_when_running_from_the_store

printf '\n=== Results: %d passed, %d failed ===\n' "$PASS" "$FAIL"
if [ $FAIL -gt 0 ]; then
    printf 'Failed tests:\n'
    printf '  - %s\n' "${FAILED_TESTS[@]}"
    exit 1
fi
exit 0
