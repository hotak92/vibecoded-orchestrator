#!/usr/bin/env bash
# check-bundled-binaries.sh — Validate every binary under launcher/dist/
# has an embedded SvelteKit frontend.
#
# Why this exists: at commit 5abb8cf (2026-04-28) a bundled binary was
# committed with no embedded `_app/immutable/assets/*` because
# `launcher/build/` was empty when `tauri build` ran. The binary
# compiled and passed all backend tests, but the webview rendered
# "Could not connect to localhost" at startup. Every user who pulled
# the repo got a non-functional launcher.
#
# This script catches that condition by `strings`-grepping each
# bundled binary for SvelteKit asset references. Wire it into:
#   - pre-commit hook (refuse to commit a broken binary)
#   - GitHub Actions on PRs that touch launcher/dist/
#   - scripts/build-bundled-launcher.sh (already does this inline)
#
# Exit codes:
#   0 = all binaries OK (or no binaries to check)
#   1 = at least one binary lacks embedded frontend
#   2 = `strings` not available — can't validate (treated as failure
#       in CI; pass-through locally if you know what you're doing)

set -euo pipefail

# Some dev environments wrap common shell tools (find, grep, strings)
# via lean-ctx for output compression. Those wrappers occasionally
# misinterpret arg lists or pipelines, causing this script to silently
# under-report. Use bare tools: disable the shim + prefer absolute paths.
export LEAN_CTX_OFF=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$REPO_ROOT/launcher/dist"

if [ ! -d "$DIST_DIR" ]; then
    echo "[check-bundled] $DIST_DIR does not exist — nothing to check."
    exit 0
fi

if ! command -v strings >/dev/null 2>&1; then
    echo "[check-bundled] FATAL: \`strings\` not found on PATH (binutils required)." >&2
    exit 2
fi

# Resolve absolute paths so lean-ctx's `BASH_ENV`-injected aliases for
# find/grep/strings can't shadow us. The shim breaks pipelines like
# `strings | grep -c PATTERN` by reformatting the args mid-flight.
STRINGS_BIN="$(command -v strings)"
GREP_BIN="$(command -v grep)"
for p in /usr/bin/strings /bin/strings; do [ -x "$p" ] && STRINGS_BIN="$p" && break; done
for p in /usr/bin/grep /bin/grep; do [ -x "$p" ] && GREP_BIN="$p" && break; done

# Find every executable under launcher/dist/. Use the full path to GNU
# find rather than `find` on PATH — dev environments sometimes alias
# `find` to `bfs` (lean-ctx) which silently returns no results on the
# pattern we use here, causing this script to falsely report "nothing
# to check" and let a broken binary slip through.
FIND_BIN="/usr/bin/find"
if [ ! -x "$FIND_BIN" ]; then
    FIND_BIN="$(command -v find)"
fi
mapfile -t binaries < <("$FIND_BIN" "$DIST_DIR" -type f \( -name 'vct-launcher' -o -name 'vct-launcher.exe' -o -name 'launcher' \) 2>/dev/null)

if [ ${#binaries[@]} -eq 0 ]; then
    echo "[check-bundled] No bundled binaries found under $DIST_DIR — nothing to check."
    exit 0
fi

failures=0
for bin in "${binaries[@]}"; do
    rel="${bin#"$REPO_ROOT/"}"
    if [ ! -x "$bin" ]; then
        # On Windows builds tracked from Linux, the .exe may not have
        # +x; that's fine, `strings` reads bytes either way.
        true
    fi

    count="$("$STRINGS_BIN" "$bin" 2>/dev/null | "$GREP_BIN" -c '_app/immutable/assets' || true)"
    if [ "${count:-0}" -lt 5 ]; then
        echo "[check-bundled] FAIL: $rel has $count embedded frontend asset refs (expected >=5)." >&2
        echo "                Frontend was NOT embedded. Rebuild with:" >&2
        echo "                  cd $REPO_ROOT && bash scripts/build-bundled-launcher.sh" >&2
        failures=$((failures + 1))
    else
        echo "[check-bundled] OK:   $rel ($count asset refs)"
    fi
done

if [ "$failures" -gt 0 ]; then
    echo "" >&2
    echo "[check-bundled] $failures broken binary(ies) found." >&2
    exit 1
fi

echo "[check-bundled] All ${#binaries[@]} bundled binary(ies) have embedded frontend."
exit 0
