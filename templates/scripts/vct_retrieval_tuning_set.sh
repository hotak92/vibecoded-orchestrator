#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_retrieval_tuning_set.sh — write a single retrieval tuning value
# (or replace the whole block) into <vct_root_dir>/retrieval-tuning.toml.
#
# v0.2.22 Item #13 (2026-05-20). The hub currently has no PUT/POST
# write endpoint for retrieval tuning — the launcher's Tauri command
# is the only authenticated writer surface. Headless callers update the
# TOML file directly with this script; the hub re-reads the file on
# every /config response (no in-memory cache for these values), so the
# next resolver call observes the change immediately. If the launcher
# GUI is open, it polls on focus / load and will catch up.
#
# Validation matches the Rust gate exactly:
#   - every value MUST be a number in [0.0, 1.0]
#   - kg_tier_min < kg_tier_single_chunk < kg_tier_three_chunks
#     < kg_tier_full  (strict)
# Violations exit non-zero WITHOUT writing.
#
# Usage:
#   vct_retrieval_tuning_set.sh --field NAME --value V
#       Set a single field. Other fields are read from the existing
#       file (or the calibrated defaults if the file is absent),
#       validated together, and the whole block is rewritten.
#
#   vct_retrieval_tuning_set.sh --reset
#       Reset the whole block to the calibrated defaults.
#
# Exit codes:
#   0  success
#   1  write failed (disk error)
#   2  validation failed (out-of-range / ordering)
#   4  unknown field name
#   64 usage error
#
# F-9 (v0.2.75): this .sh is the REFERENCE for the exit-code contract. The
# .ps1 sibling used to declare [ValidateSet]/Mandatory on -Field/-Value, so
# PowerShell's binder turned an unknown-field / missing-arg into a
# terminating error (exit 1), never the 4 / 64 emitted here. The sibling was
# brought into line (explicit checks replacing the binding) rather than
# softening this header — see vct_retrieval_tuning_set.ps1's F-9 note.
#
# Note: this script writes the file directly with the same atomic
# tmp+rename posture the Rust writer uses. There is intentionally no
# hub-roundtrip write path — the hub is read-only for these values
# in v0.2.22. A future revision can add a /api/v1/retrieval_tuning
# PUT endpoint without changing the file format.

set -euo pipefail

err() { printf '[vct-retrieval-tuning] %s\n' "$*" >&2; }

# Canonical field set (matches the Rust struct and the FE panel).
# Ordering MATTERS — we use it both for argv validation and for the
# rewritten file's stable field order.
_FIELDS=(
    code_graph_score_floor
    kg_tier_min
    kg_tier_single_chunk
    kg_tier_three_chunks
    kg_tier_full
)

# Defaults from knowledge/concepts/score-driven-retrieval-tiers.md.
# DO NOT CHANGE without updating Rust + Svelte panel in lockstep.
_DEFAULT_code_graph_score_floor=0.35
_DEFAULT_kg_tier_min=0.42
_DEFAULT_kg_tier_single_chunk=0.55
_DEFAULT_kg_tier_three_chunks=0.65
_DEFAULT_kg_tier_full=0.75

usage() {
    cat >&2 <<EOF
Usage:
  $0 --field NAME --value V
  $0 --reset

Fields: code_graph_score_floor, kg_tier_min, kg_tier_single_chunk,
        kg_tier_three_chunks, kg_tier_full

Exit codes:
  0  success
  1  disk error
  2  validation failed (out-of-range / ordering)
  4  unknown field name
  64 usage error
EOF
}

# ── Field-name validator ────────────────────────────────────────────────
is_known_field() {
    local needle="$1"
    for f in "${_FIELDS[@]}"; do
        if [[ "$f" == "$needle" ]]; then
            return 0
        fi
    done
    return 1
}

# ── TOML file path ──────────────────────────────────────────────────────
toml_path() {
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    printf '%s/retrieval-tuning.toml\n' "$state_dir"
}

# ── Validate + write (python3 mandatory — TOML in pure bash is fragile) ─
write_full_block() {
    local code_graph_score_floor="$1"
    local kg_tier_min="$2"
    local kg_tier_single_chunk="$3"
    local kg_tier_three_chunks="$4"
    local kg_tier_full="$5"
    local target
    target=$(toml_path)

    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 required for write (TOML formatting); not on PATH"
        return 1
    fi

    VCT_TUNING_TARGET="$target" \
    VCT_TUNING_VALUES="${code_graph_score_floor},${kg_tier_min},${kg_tier_single_chunk},${kg_tier_three_chunks},${kg_tier_full}" \
    python3 - <<'PY'
import os, sys, tempfile

target = os.environ['VCT_TUNING_TARGET']
parts = os.environ['VCT_TUNING_VALUES'].split(',')
if len(parts) != 5:
    sys.stderr.write('[vct-retrieval-tuning] internal: expected 5 values\n')
    sys.exit(2)
try:
    vals = [float(p) for p in parts]
except ValueError as e:
    sys.stderr.write(f'[vct-retrieval-tuning] non-numeric value: {e}\n')
    sys.exit(2)

(cgf, kt_min, kt_sc, kt_3c, kt_full) = vals

# Range check.
for name, v in zip(
    ('code_graph_score_floor', 'kg_tier_min', 'kg_tier_single_chunk',
     'kg_tier_three_chunks', 'kg_tier_full'),
    vals,
):
    if not (v == v):  # NaN
        sys.stderr.write(f'[vct-retrieval-tuning] {name} is NaN\n')
        sys.exit(2)
    if not (0.0 <= v <= 1.0):
        sys.stderr.write(
            f'[vct-retrieval-tuning] {name}={v} not in [0, 1]\n'
        )
        sys.exit(2)

# Strict ordering across the four KG tiers.
if not (kt_min < kt_sc):
    sys.stderr.write(
        f'[vct-retrieval-tuning] kg_tier_min ({kt_min}) must be < kg_tier_single_chunk ({kt_sc})\n'
    )
    sys.exit(2)
if not (kt_sc < kt_3c):
    sys.stderr.write(
        f'[vct-retrieval-tuning] kg_tier_single_chunk ({kt_sc}) must be < kg_tier_three_chunks ({kt_3c})\n'
    )
    sys.exit(2)
if not (kt_3c < kt_full):
    sys.stderr.write(
        f'[vct-retrieval-tuning] kg_tier_three_chunks ({kt_3c}) must be < kg_tier_full ({kt_full})\n'
    )
    sys.exit(2)

body = (
    f'code_graph_score_floor = {cgf}\n'
    f'kg_tier_min = {kt_min}\n'
    f'kg_tier_single_chunk = {kt_sc}\n'
    f'kg_tier_three_chunks = {kt_3c}\n'
    f'kg_tier_full = {kt_full}\n'
)

os.makedirs(os.path.dirname(target), exist_ok=True)
# Atomic write: tmp + rename. Same posture as the Rust writer.
fd, tmp = tempfile.mkstemp(
    prefix='retrieval-tuning.', suffix='.tmp',
    dir=os.path.dirname(target),
)
try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(body)
    os.replace(tmp, target)
except Exception as e:
    try:
        os.remove(tmp)
    except OSError:
        pass
    sys.stderr.write(f'[vct-retrieval-tuning] write failed: {e}\n')
    sys.exit(1)
PY
    return $?
}

# Read the existing TOML (or defaults if absent / unreadable). Pure
# python3, mirrors the get script's fallback reader. Outputs the 5
# values space-separated on stdout in the canonical _FIELDS order.
read_existing_or_defaults() {
    if ! command -v python3 >/dev/null 2>&1; then
        # No python3: emit defaults verbatim.
        printf '%s %s %s %s %s\n' \
            "$_DEFAULT_code_graph_score_floor" \
            "$_DEFAULT_kg_tier_min" \
            "$_DEFAULT_kg_tier_single_chunk" \
            "$_DEFAULT_kg_tier_three_chunks" \
            "$_DEFAULT_kg_tier_full"
        return 0
    fi
    VCT_TUNING_PATH="$(toml_path)" python3 - <<'PY'
import os, sys
path = os.environ['VCT_TUNING_PATH']
defaults = {
    'code_graph_score_floor': 0.35,
    'kg_tier_min': 0.42,
    'kg_tier_single_chunk': 0.55,
    'kg_tier_three_chunks': 0.65,
    'kg_tier_full': 0.75,
}
parsed = {}
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None
if tomllib is not None and os.path.exists(path):
    try:
        with open(path, 'rb') as f:
            parsed = tomllib.load(f)
    except Exception:
        parsed = {}
out = {k: parsed.get(k, defaults[k]) for k in defaults}
print(
    f"{out['code_graph_score_floor']} {out['kg_tier_min']} "
    f"{out['kg_tier_single_chunk']} {out['kg_tier_three_chunks']} "
    f"{out['kg_tier_full']}"
)
PY
}

# ── Main ────────────────────────────────────────────────────────────────
main() {
    if [[ $# -lt 1 ]]; then
        usage
        exit 64
    fi

    local field="" value="" reset=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -h|--help)
                usage
                exit 0
                ;;
            --reset)
                reset=1
                shift
                ;;
            --field)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    err "--field requires NAME"
                    exit 64
                fi
                field="$2"
                shift 2
                ;;
            --value)
                if [[ $# -lt 2 || -z "${2:-}" ]]; then
                    err "--value requires V"
                    exit 64
                fi
                value="$2"
                shift 2
                ;;
            *)
                err "unknown option: $1"
                exit 64
                ;;
        esac
    done

    if [[ $reset -eq 1 ]]; then
        if [[ -n "$field" || -n "$value" ]]; then
            err "--reset is mutually exclusive with --field / --value"
            exit 64
        fi
        write_full_block \
            "$_DEFAULT_code_graph_score_floor" \
            "$_DEFAULT_kg_tier_min" \
            "$_DEFAULT_kg_tier_single_chunk" \
            "$_DEFAULT_kg_tier_three_chunks" \
            "$_DEFAULT_kg_tier_full"
        return $?
    fi

    if [[ -z "$field" || -z "$value" ]]; then
        err "either --reset or both --field NAME --value V required"
        exit 64
    fi
    if ! is_known_field "$field"; then
        err "unknown field: $field"
        exit 4
    fi

    # Read existing values, swap the named field, validate + write.
    local existing
    existing=$(read_existing_or_defaults)
    local -a vals
    read -r -a vals <<<"$existing"
    if [[ ${#vals[@]} -ne 5 ]]; then
        err "internal: existing-values reader returned ${#vals[@]} values, expected 5"
        exit 1
    fi

    # Override the named field. Index lookup mirrors _FIELDS order.
    local i=0 idx=-1
    for f in "${_FIELDS[@]}"; do
        if [[ "$f" == "$field" ]]; then
            idx=$i
            break
        fi
        i=$((i + 1))
    done
    if [[ $idx -lt 0 ]]; then
        err "internal: field $field passed is_known_field but no index"
        exit 1
    fi
    vals[$idx]="$value"

    write_full_block "${vals[0]}" "${vals[1]}" "${vals[2]}" "${vals[3]}" "${vals[4]}"
    return $?
}

main "$@"
