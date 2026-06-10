#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Regression test for M-P0-6 (v0.2.53):
# scripts/post-install-launcher.sh probes node / npm / pnpm via
# _ensure_path_for_tool with a hardcoded candidate path list. Before
# v0.2.53 the list omitted /opt/homebrew/bin/... (Apple Silicon
# Homebrew's default install location); Apple-Silicon users showed
# `node: no` even with brew-installed Node.
#
# This test asserts:
# 1. The probe lists for node, npm, pnpm include `/opt/homebrew/bin/<tool>`.
# 2. The probe lists for node, npm, pnpm include `/usr/local/bin/<tool>`
#    (Intel-Mac brew + Linux fallback — retained).
# 3. The Apple-Silicon homebrew shellenv block is present.
# 4. Smoke: with a fake PATH that contains ONLY a `/opt/homebrew/bin/`
#    pointing at a stubbed `node`, `_ensure_path_for_tool node`
#    succeeds.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="$REPO_ROOT/scripts/post-install-launcher.sh"

if [ ! -f "$TARGET" ]; then
    echo "FAIL: $TARGET not found"
    exit 1
fi

# 1. Grep checks.
if ! grep -q '"/opt/homebrew/bin/node"' "$TARGET"; then
    echo "FAIL: $TARGET node probe does not include /opt/homebrew/bin/node (M-P0-6)"
    exit 1
fi
if ! grep -q '"/opt/homebrew/bin/npm"' "$TARGET"; then
    echo "FAIL: $TARGET npm probe does not include /opt/homebrew/bin/npm (M-P0-6)"
    exit 1
fi
if ! grep -q '"/opt/homebrew/bin/pnpm"' "$TARGET"; then
    echo "FAIL: $TARGET pnpm probe does not include /opt/homebrew/bin/pnpm (M-P0-6)"
    exit 1
fi

# 2. Intel + Linux fallback must remain.
if ! grep -q '"/usr/local/bin/node"' "$TARGET"; then
    echo "FAIL: $TARGET node probe missing /usr/local/bin/node (regression)"
    exit 1
fi

# 3. Apple-Silicon homebrew shellenv block.
if ! grep -q '/opt/homebrew/bin/brew shellenv' "$TARGET"; then
    echo "FAIL: $TARGET does not re-source /opt/homebrew/bin/brew shellenv (M-P0-6)"
    exit 1
fi

# 4. Smoke: extract _ensure_path_for_tool + _resolves_to_binary defs
#    and run them against a fake homebrew-only Apple-Silicon layout.
SANDBOX="$(mktemp -d -t test-apple-silicon-probe.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT
FAKE_BIN="$SANDBOX/opt/homebrew/bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/node" <<'STUB'
#!/usr/bin/env bash
echo "v20.0.0"
STUB
chmod +x "$FAKE_BIN/node"

# Pull the relevant function defs from the target script using sed
# range-pattern. Both functions live inside _check_prerequisites; we
# extract from `_resolves_to_binary() {` through end of
# `_ensure_path_for_tool` definition.
HELPERS="$SANDBOX/helpers.sh"
awk '
    /^    _resolves_to_binary\(\) \{/      { capture=1 }
    capture                                { print }
    /^    \}$/ && capture && in_ensure     { capture=0; in_ensure=0 }
    /^    _ensure_path_for_tool\(\) \{/    { in_ensure=1 }
' "$TARGET" > "$HELPERS"

if [ ! -s "$HELPERS" ]; then
    echo "FAIL: could not extract _resolves_to_binary + _ensure_path_for_tool from $TARGET"
    exit 1
fi

# Wrap helpers in a runnable script. The script overrides PATH so
# `command -v node` can NOT find node anywhere else, then calls
# _ensure_path_for_tool with the /opt/homebrew/bin candidate.
RUNNER="$SANDBOX/run.sh"
cat > "$RUNNER" <<RUNSH
#!/usr/bin/env bash
set -uo pipefail
# Empty PATH first — _ensure_path_for_tool augments PATH itself.
export PATH="/usr/bin"
unset -f node 2>/dev/null || true
$(cat "$HELPERS")

if _ensure_path_for_tool node "$FAKE_BIN/node"; then
    if command -v node >/dev/null 2>&1; then
        node_path="\$(command -v node)"
        case "\$node_path" in
            "$FAKE_BIN/node") echo "RESOLVED_TO_FAKE"; exit 0 ;;
            *) echo "WRONG_PATH: \$node_path"; exit 1 ;;
        esac
    else
        echo "POST_AUGMENT_COMMAND_V_FAILED"; exit 1
    fi
else
    echo "ENSURE_PATH_RETURNED_NONZERO"; exit 1
fi
RUNSH

chmod +x "$RUNNER"
OUTPUT="$(bash "$RUNNER" 2>&1)"
RC=$?

if [ "$RC" -ne 0 ]; then
    echo "FAIL: _ensure_path_for_tool failed to resolve /opt/homebrew/bin/node"
    echo "  Output: $OUTPUT"
    exit 1
fi
if ! printf '%s' "$OUTPUT" | grep -q "RESOLVED_TO_FAKE"; then
    echo "FAIL: probe smoke unexpected output: $OUTPUT"
    exit 1
fi

echo "PASS: Apple Silicon /opt/homebrew/bin/{node,npm,pnpm} probes present"
exit 0
