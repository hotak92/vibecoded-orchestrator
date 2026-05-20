#!/usr/bin/env bash
# check_managed_paths_cross_language.sh
#
# Closes follow-up #5 — the third edge of the consistency triangle for
# `orchestrator-managed-paths.txt`. Pre-fix: both languages had their
# own hard-coded `EXPECTED_MANAGED_PATHS` literal that they tested
# against, but nothing pinned the EXPECTED literals against each other.
# A reviewer adding a new path to the .txt + Python test could forget
# the Rust test (or vice versa) and ship a quietly-mismatched
# allowlist.
#
# This script runs at CI time after the cargo + python jobs and
# performs a language-independent triangulation:
#
#   1. Parse `orchestrator-managed-paths.txt` from a small standalone
#      Python implementation of the parse rules (NOT importing
#      install.py — independent re-derivation).
#   2. Have the Rust binary print its loaded list (added below as a
#      cargo test that emits to stdout in --nocapture mode).
#   3. `diff` the two outputs.
#
# A drift here means EITHER one of the parsers has a bug OR one of the
# language-side EXPECTED constants is wrong AND the matching parser
# was edited to "fix" it — both classes of error this script catches.

set -euo pipefail

cd "$(dirname "$0")/../.."

echo "::group::Independent Python parse"
python3 - <<'PY'
import sys
from pathlib import Path

text = Path("orchestrator-managed-paths.txt").read_text(encoding="utf-8")
if text.startswith("﻿"):
    text = text[1:]
out = [
    line.strip()
    for line in text.splitlines()
    if line.strip() and not line.strip().startswith("#")
]
for entry in out:
    print(entry)
PY
echo "::endgroup::"

# Run the Python re-parser separately so we can capture+diff.
python3 - <<'PY' > /tmp/managed-paths.python.txt
from pathlib import Path
text = Path("orchestrator-managed-paths.txt").read_text(encoding="utf-8")
if text.startswith("﻿"):
    text = text[1:]
for line in text.splitlines():
    s = line.strip()
    if s and not s.startswith("#"):
        print(s)
PY

# Run the Rust side. We exercise the same parser the launcher uses by
# invoking the cargo test that prints the resolved list. The test
# (added in this commit) is gated by an env var so normal cargo test
# runs don't print to stdout.
#
# NOTE (v0.2.22 D12 fix): we explicitly DO NOT use --release here.
# After the Cargo workspace migration (commit d3c5e6e, 2026-05-20 01:09),
# the dep crate `vct-launcher-core` gates its test-only helpers
# (Db::open_in_memory, secrets::test_serialize) on
# `#[cfg(any(test, debug_assertions))]`. Cross-crate `cfg(test)` does
# NOT propagate from consumer to dep, AND `--release` turns
# `debug_assertions` off — together they configure-out those helpers,
# which the consumer's `#[cfg(test)] mod tests` block then can't
# resolve (77 compile errors). The fully-correct fix is to add a
# Cargo `test-helpers` feature on vct-launcher-core and have consumers
# declare it in dev-dependencies; until that lands, debug-profile
# tests cover the parser correctly (ORCHESTRATOR_MANAGED_PATHS is a
# const slice — release vs debug profile produces identical parser
# behaviour). See knowledge/concepts/v0.2.22-release-2026-05-20.md
# Lesson 2 + plan §D12 in .claude/context/plans/v0.2.22-deferred-followups.md.
echo "::group::Rust parse via cargo test"
cargo test --lib --manifest-path launcher/src-tauri/Cargo.toml \
    print_managed_paths_for_ci \
    -- --nocapture --ignored \
    2>&1 \
    | sed -n 's/^MANAGED_PATH: //p' \
    > /tmp/managed-paths.rust.txt
cat /tmp/managed-paths.rust.txt
echo "::endgroup::"

echo "::group::Diff"
if diff -u /tmp/managed-paths.python.txt /tmp/managed-paths.rust.txt; then
    echo "✓ Python parser and Rust parser agree on orchestrator-managed-paths.txt contents."
else
    echo "::error::Python and Rust managed-paths parsers DISAGREE." \
         "Either one of the parsers has a bug, or the .txt has been" \
         "edited to a shape one parser handles and the other doesn't."
    exit 1
fi
echo "::endgroup::"
