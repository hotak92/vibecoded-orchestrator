#!/usr/bin/env bash
# v0.2.46 Part 2 pre-ship gate runner — V47-A through V47-G-final.
#
# Verifies that the third-party project adoption mode (Part 2) surfaces
# all landed correctly before tagging v0.2.46.  Covers Gates 19–27.
#
# This script is ADDITIVE to scripts/v0246-pre-ship-check.sh (Part 1
# gates 1–18).  Run both before tagging:
#
#   bash scripts/v0246-pre-ship-check.sh
#   bash scripts/v0246-part2-pre-ship-check.sh
#
# Gates 19–27 map to V47-A through V47-G-final agent deliverables:
#
#   Gate 19: V47-A managed-block schema present
#            (tests/test_v0246_v47a_settings_managed_block.py)
#   Gate 20: V47-B symlink_handler module + install.py import
#   Gate 21: V47-C secrets_audit module + vct-hub secrets_api endpoint
#   Gate 22: V47-D _venv_triage accepts force_rebuild kwarg
#   Gate 23: V47-E _scan_foreign_compose_files exists
#   Gate 24: V47-F _resolve_project_name_for_adopt + non-destructiveness
#            gate test present
#   Gate 25: V47-G-final _detect_third_party_project +
#            _prompt_adopt_decision + _print_adopt_dry_run_manifest exist
#   Gate 26: V47-G-stub contract tests + all V47-A-G test files exist
#            and pass
#   Gate 27: Rust `cargo check --lib -p vct-hub` clean
#
# Usage:
#   bash scripts/v0246-part2-pre-ship-check.sh
#
# Exit code: 0 = all gates pass, 1 = one or more gates failed.
#
# Requires: cargo (or rustup run 1.95 cargo), python3, pytest.
# Run from the repo root.

set -uo pipefail

EXPECTED_VERSION="0.2.46"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RESET='\033[0m'

pass_count=0
fail_count=0
warn_count=0
declare -a failures=()
declare -a warnings=()

gate_pass() {
    local name="$1"
    printf "${GREEN}[PASS]${RESET} %s\n" "$name"
    pass_count=$((pass_count + 1))
}

gate_fail() {
    local name="$1"
    local detail="${2:-}"
    printf "${RED}[FAIL]${RESET} %s\n" "$name"
    [ -n "$detail" ] && printf "       %s\n" "$detail"
    fail_count=$((fail_count + 1))
    failures+=("$name")
}

gate_warn() {
    local name="$1"
    local detail="${2:-}"
    printf "${YELLOW}[WARN]${RESET} %s\n" "$name"
    [ -n "$detail" ] && printf "       %s\n" "$detail"
    warn_count=$((warn_count + 1))
    warnings+=("$name")
}

echo ""
echo "============================================================"
echo " v${EXPECTED_VERSION} Part 2 pre-ship gate check (V47-A through V47-G-final)"
echo " Date: $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "============================================================"
echo ""

cd "$REPO_ROOT"

# ── Resolve Python / pytest invocation ───────────────────────────────────────
# Same probe order as scripts/v0246-pre-ship-check.sh.
_PYTEST_PY=""
_PYTEST_CMD=()
if [ -n "${PYTEST:-}" ]; then
    case "$(basename -- "$PYTEST")" in
        pytest|pytest-*|*-pytest) _PYTEST_CMD=("$PYTEST") ;;
        *)                        _PYTEST_PY="$PYTEST" ;;
    esac
elif [ -x ".venv/bin/python" ]; then
    _PYTEST_PY=".venv/bin/python"
elif [ -x ".venv/bin/python3" ]; then
    _PYTEST_PY=".venv/bin/python3"
elif [ -x "claude_mcp_servers/.venv/bin/python" ]; then
    _PYTEST_PY="claude_mcp_servers/.venv/bin/python"
elif [ -x "../VCO_dev/.venv/bin/python" ]; then
    _PYTEST_PY="../VCO_dev/.venv/bin/python"
elif command -v pytest >/dev/null 2>&1; then
    _PYTEST_PY=""
else
    _PYTEST_PY="python3"
fi
if [ ${#_PYTEST_CMD[@]} -eq 0 ]; then
    if [ -n "$_PYTEST_PY" ]; then
        _PYTEST_CMD=("$_PYTEST_PY" -m pytest)
    else
        _PYTEST_CMD=(pytest)
    fi
fi

# Resolve a Python interpreter for non-pytest import checks.
if [ -n "$_PYTEST_PY" ]; then
    _IMPORT_PY=("$_PYTEST_PY")
else
    _IMPORT_PY=(python3)
fi

# ── Resolve cargo invocation (prefer rustup 1.95 to satisfy MSRV) ────────────
if command -v rustup >/dev/null 2>&1 && rustup toolchain list 2>/dev/null | grep -q "^1\.95"; then
    _CARGO=(rustup run 1.95 cargo)
else
    _CARGO=(cargo)
fi

echo "--- V0.2.46 Part 2 gates (V47-A through V47-G-final) ---"
echo ""

# ── Gate 19: V47-A managed-block schema present ───────────────────────────────
# The sentinel key `_vco_managed_keys` must appear in both the test file
# (as a constant assertion) and in install.py (as the production value).
echo "  [Gate 19: V47-A managed-block schema...]"
declare -a g19_failures=()

if ! grep -q "_vco_managed_keys" tests/test_v0246_v47a_settings_managed_block.py 2>/dev/null; then
    g19_failures+=("tests/test_v0246_v47a_settings_managed_block.py: no _vco_managed_keys reference (V47-A test not landed?)")
fi

if ! grep -q "_VCO_MANAGED_KEYS_SENTINEL\|_vco_managed_keys" install.py 2>/dev/null; then
    g19_failures+=("install.py: no _VCO_MANAGED_KEYS_SENTINEL / _vco_managed_keys (V47-A production code not landed?)")
fi

if [ "${#g19_failures[@]}" -eq 0 ]; then
    # Also run the test file to confirm it passes.
    if [ -f tests/test_v0246_v47a_settings_managed_block.py ]; then
        if "${_PYTEST_CMD[@]}" tests/test_v0246_v47a_settings_managed_block.py -q --tb=short \
                > /tmp/v0246p2-gate19.log 2>&1; then
            gate_pass "Gate 19: V47-A managed-block schema (_vco_managed_keys) — tests pass"
        else
            gate_fail "Gate 19: V47-A managed-block schema" \
                "Tests failed — see /tmp/v0246p2-gate19.log"
        fi
    else
        gate_fail "Gate 19: V47-A managed-block schema" \
            "tests/test_v0246_v47a_settings_managed_block.py missing"
    fi
else
    gate_fail "Gate 19: V47-A managed-block schema" \
        "${g19_failures[*]}"
fi

# ── Gate 20: V47-B symlink_handler module + install.py import ────────────────
echo "  [Gate 20: V47-B symlink_handler module...]"
declare -a g20_failures=()

if [ ! -f vco_lib/symlink_handler.py ]; then
    g20_failures+=("vco_lib/symlink_handler.py missing (V47-B not landed?)")
else
    # Verify the three helper functions documented in the spec are present.
    for fn in is_symlink_blocking compute_vco_new_path emit_symlink_deferral; do
        if ! grep -q "^def ${fn}" vco_lib/symlink_handler.py 2>/dev/null; then
            g20_failures+=("vco_lib/symlink_handler.py: ${fn} not found")
        fi
    done
    # Verify install.py imports from symlink_handler.
    if ! grep -q "symlink_handler" install.py 2>/dev/null; then
        g20_failures+=("install.py: no import from vco_lib.symlink_handler (V47-B wiring missing?)")
    fi
fi

if [ "${#g20_failures[@]}" -eq 0 ]; then
    # Spot-check import cleanly.
    if "${_IMPORT_PY[@]}" -c \
            "from vco_lib.symlink_handler import is_symlink_blocking, compute_vco_new_path, emit_symlink_deferral" \
            > /tmp/v0246p2-gate20-import.log 2>&1; then
        gate_pass "Gate 20: V47-B symlink_handler module + install.py import"
    else
        gate_fail "Gate 20: V47-B symlink_handler module" \
            "Import failed — see /tmp/v0246p2-gate20-import.log"
    fi
else
    gate_fail "Gate 20: V47-B symlink_handler module" \
        "${g20_failures[*]}"
fi

# ── Gate 21: V47-C secrets_audit module + vct-hub secrets_api endpoint ────────
echo "  [Gate 21: V47-C secrets_audit module + vct-hub secrets_api...]"
declare -a g21_failures=()

if [ ! -f vco_lib/secrets_audit.py ]; then
    g21_failures+=("vco_lib/secrets_audit.py missing (V47-C not landed?)")
else
    for fn in audit_env_secrets is_secret_shaped_env_key; do
        if ! grep -q "^def ${fn}" vco_lib/secrets_audit.py 2>/dev/null; then
            g21_failures+=("vco_lib/secrets_audit.py: ${fn} not found")
        fi
    done
fi

# vct-hub secrets_api endpoint: module declared in lib.rs, route in server.rs.
if ! grep -q "secrets_api" launcher/src-tauri/vct-hub/src/lib.rs 2>/dev/null; then
    g21_failures+=("launcher/src-tauri/vct-hub/src/lib.rs: secrets_api module not declared")
fi
if ! grep -q "secrets/migrate" launcher/src-tauri/vct-hub/src/secrets_api.rs 2>/dev/null; then
    g21_failures+=("launcher/src-tauri/vct-hub/src/secrets_api.rs: /secrets/migrate route not found")
fi

if [ "${#g21_failures[@]}" -eq 0 ]; then
    # Import check on the Python module.
    if "${_IMPORT_PY[@]}" -c \
            "from vco_lib.secrets_audit import audit_env_secrets, is_secret_shaped_env_key" \
            > /tmp/v0246p2-gate21-import.log 2>&1; then
        gate_pass "Gate 21: V47-C secrets_audit module + vct-hub /secrets/migrate endpoint"
    else
        gate_fail "Gate 21: V47-C secrets_audit module" \
            "Import failed — see /tmp/v0246p2-gate21-import.log"
    fi
else
    gate_fail "Gate 21: V47-C secrets_audit module + vct-hub secrets_api" \
        "${g21_failures[*]}"
fi

# ── Gate 22: V47-D _venv_triage accepts force_rebuild kwarg ──────────────────
echo "  [Gate 22: V47-D _venv_triage force_rebuild kwarg...]"
# Two checks:
# 1. Source text: `force_rebuild` kwarg present in function signature.
# 2. Test file runs cleanly.
declare -a g22_failures=()

if ! grep -q "force_rebuild" install.py 2>/dev/null; then
    g22_failures+=("install.py: force_rebuild not found in _venv_triage signature (V47-D not landed?)")
fi

if [ ! -f tests/test_v0246_v47d_venv_adopt_guard.py ]; then
    g22_failures+=("tests/test_v0246_v47d_venv_adopt_guard.py missing")
fi

if [ "${#g22_failures[@]}" -eq 0 ]; then
    if "${_PYTEST_CMD[@]}" tests/test_v0246_v47d_venv_adopt_guard.py -q --tb=short \
            > /tmp/v0246p2-gate22.log 2>&1; then
        gate_pass "Gate 22: V47-D _venv_triage force_rebuild kwarg — tests pass"
    else
        gate_fail "Gate 22: V47-D _venv_triage force_rebuild" \
            "Tests failed — see /tmp/v0246p2-gate22.log"
    fi
else
    gate_fail "Gate 22: V47-D _venv_triage force_rebuild kwarg" \
        "${g22_failures[*]}"
fi

# ── Gate 23: V47-E _scan_foreign_compose_files exists ────────────────────────
echo "  [Gate 23: V47-E _scan_foreign_compose_files...]"
declare -a g23_failures=()

if ! grep -q "_scan_foreign_compose_files" install.py 2>/dev/null; then
    g23_failures+=("install.py: _scan_foreign_compose_files not found (V47-E not landed?)")
fi

if [ ! -f tests/test_v0246_v47e_foreign_compose_scan.py ]; then
    g23_failures+=("tests/test_v0246_v47e_foreign_compose_scan.py missing")
fi

if [ "${#g23_failures[@]}" -eq 0 ]; then
    if "${_PYTEST_CMD[@]}" tests/test_v0246_v47e_foreign_compose_scan.py -q --tb=short \
            > /tmp/v0246p2-gate23.log 2>&1; then
        gate_pass "Gate 23: V47-E _scan_foreign_compose_files — tests pass"
    else
        gate_fail "Gate 23: V47-E _scan_foreign_compose_files" \
            "Tests failed — see /tmp/v0246p2-gate23.log"
    fi
else
    gate_fail "Gate 23: V47-E _scan_foreign_compose_files" \
        "${g23_failures[*]}"
fi

# ── Gate 24: V47-F _resolve_project_name_for_adopt + non-destructiveness ──────
echo "  [Gate 24: V47-F _resolve_project_name_for_adopt + non-destructiveness gate...]"
declare -a g24_failures=()

if ! grep -q "_resolve_project_name_for_adopt" install.py 2>/dev/null; then
    g24_failures+=("install.py: _resolve_project_name_for_adopt not found (V47-F not landed?)")
fi

if [ ! -f tests/test_v0246_v47f_project_name_precedence.py ]; then
    g24_failures+=("tests/test_v0246_v47f_project_name_precedence.py missing")
fi

if [ "${#g24_failures[@]}" -eq 0 ]; then
    if "${_PYTEST_CMD[@]}" tests/test_v0246_v47f_project_name_precedence.py -q --tb=short \
            > /tmp/v0246p2-gate24.log 2>&1; then
        gate_pass "Gate 24: V47-F _resolve_project_name_for_adopt — tests pass"
    else
        gate_fail "Gate 24: V47-F _resolve_project_name_for_adopt" \
            "Tests failed — see /tmp/v0246p2-gate24.log"
    fi
else
    gate_fail "Gate 24: V47-F _resolve_project_name_for_adopt + non-destructiveness" \
        "${g24_failures[*]}"
fi

# ── Gate 25: V47-G-final _detect_third_party_project + _prompt_adopt_decision
#            + _print_adopt_dry_run_manifest all exist ────────────────────────
echo "  [Gate 25: V47-G-final detection + prompt + dry-run functions...]"
declare -a g25_failures=()

for fn in _detect_third_party_project _prompt_adopt_decision _print_adopt_dry_run_manifest; do
    if ! grep -q "^def ${fn}" install.py 2>/dev/null; then
        g25_failures+=("install.py: ${fn} not found (V47-G-final not fully landed?)")
    fi
done

if [ ! -f tests/test_v0246_v47gfinal_detection_and_prompt.py ]; then
    g25_failures+=("tests/test_v0246_v47gfinal_detection_and_prompt.py missing")
fi

if [ "${#g25_failures[@]}" -eq 0 ]; then
    if "${_PYTEST_CMD[@]}" tests/test_v0246_v47gfinal_detection_and_prompt.py -q --tb=short \
            > /tmp/v0246p2-gate25.log 2>&1; then
        gate_pass "Gate 25: V47-G-final _detect_third_party_project + _prompt_adopt_decision + _print_adopt_dry_run_manifest — tests pass"
    else
        gate_fail "Gate 25: V47-G-final detection + prompt + dry-run" \
            "Tests failed — see /tmp/v0246p2-gate25.log"
    fi
else
    gate_fail "Gate 25: V47-G-final detection + prompt + dry-run" \
        "${g25_failures[*]}"
fi

# ── Gate 26: V47-G-stub contract tests + all V47-A-G test files exist + pass ──
echo "  [Gate 26: V47-G-stub contract tests + all V47 test files...]"
declare -a g26_failures=()

# All expected V47 test files.
v47_test_files=(
    tests/test_v0246_v47a_settings_managed_block.py
    tests/test_v0246_v47b_symlink_preserve.py
    tests/test_v0246_v47c_secrets_detection.py
    tests/test_v0246_v47d_venv_adopt_guard.py
    tests/test_v0246_v47e_foreign_compose_scan.py
    tests/test_v0246_v47f_project_name_precedence.py
    tests/test_v0246_v47gstub_adopt_contract.py
    tests/test_v0246_v47gfinal_detection_and_prompt.py
)

for f in "${v47_test_files[@]}"; do
    if [ ! -f "$f" ]; then
        g26_failures+=("$f (missing)")
    fi
done

# Run stub contract tests specifically (they are the contract guarantees
# that Wave 3 and beyond must not break).
if [ ! -f tests/test_v0246_v47gstub_adopt_contract.py ]; then
    g26_failures+=("tests/test_v0246_v47gstub_adopt_contract.py (missing — V47-G-stub not landed?)")
fi

if [ "${#g26_failures[@]}" -gt 0 ]; then
    gate_fail "Gate 26: V47-G-stub contract tests + all V47 test files" \
        "Missing: ${g26_failures[*]}"
else
    # Run ALL V47 test files in one pytest invocation.
    if "${_PYTEST_CMD[@]}" "${v47_test_files[@]}" -q --tb=short \
            > /tmp/v0246p2-gate26.log 2>&1; then
        gate_pass "Gate 26: V47-G-stub contract tests + all V47-A-G test files — all pass"
    else
        gate_fail "Gate 26: V47-G-stub contract tests + all V47 test files" \
            "One or more test files failed — see /tmp/v0246p2-gate26.log"
    fi
fi

# ── Gate 27: Rust `cargo check --lib -p vct-hub` clean ───────────────────────
echo "  [Gate 27: cargo check --lib -p vct-hub (cmd: ${_CARGO[*]})...]"
if (cd launcher/src-tauri && \
        "${_CARGO[@]}" check --lib -p vct-hub > /tmp/v0246p2-gate27-cargo.log 2>&1); then
    gate_pass "Gate 27: cargo check --lib -p vct-hub clean"
else
    gate_fail "Gate 27: cargo check --lib -p vct-hub" \
        "Cargo check errors — see /tmp/v0246p2-gate27-cargo.log"
fi

echo ""
echo "============================================================"
echo " SUMMARY (Part 2 gates 19–27)"
echo "============================================================"
printf "  ${GREEN}PASS${RESET}: %d\n" "$pass_count"
printf "  ${YELLOW}WARN${RESET}: %d\n" "$warn_count"
printf "  ${RED}FAIL${RESET}: %d\n" "$fail_count"
echo ""

if [ "${#warnings[@]}" -gt 0 ]; then
    echo "Warnings:"
    for w in "${warnings[@]}"; do
        printf "  ${YELLOW}*${RESET} %s\n" "$w"
    done
    echo ""
fi

if [ "${#failures[@]}" -gt 0 ]; then
    echo "Failed gates:"
    for f in "${failures[@]}"; do
        printf "  ${RED}*${RESET} %s\n" "$f"
    done
    echo ""
    echo "Fix all failed gates before tagging v$EXPECTED_VERSION."
    echo ""
    echo "Also run: bash scripts/v0246-pre-ship-check.sh (Part 1 gates 1–18)"
    exit 1
fi

echo "All Part 2 gates PASSED."
echo "Also run: bash scripts/v0246-pre-ship-check.sh (Part 1 gates 1–18)"
exit 0
