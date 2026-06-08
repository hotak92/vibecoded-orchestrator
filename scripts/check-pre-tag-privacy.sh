#!/usr/bin/env bash
# scripts/check-pre-tag-privacy.sh — Gate 21 of pre-ship-check.
#
# Fails if the tracked tree contains any of the private-state markers
# identified in the v0.2.49 leakage audit. Run before every tag.
#
# Patterns checked:
#   - FABIO-LOCAL HTML-comment block (CLAUDE.md operational private state)
#   - commercial_workflow/ path references (private companion repo)
#   - Ombromanto / martino-X670E-Pro-RS hostnames
#   - Desktop/PROGETTI/Claude/ author-machine layout
#   - HANDOFF-*.md at repo root
#   - /home/martino in shipping code outside intentional sentinels
#
# Sentinels that ARE allowed:
#   - tests/test_launcher_leak_grep.py (sentinel inputs for the leak detector)
#   - scripts/check-install.sh (self-test regex)
#   - docs/REPO_CLEANLINESS.md (the policy doc explaining the sentinels)
#   - pyproject.toml maintainer email (AGPL author metadata)

set -euo pipefail

FAIL=0

check_pattern() {
    local description="$1"
    local pattern="$2"
    local exclude_pattern="${3:-}"
    local hits
    if [ -n "$exclude_pattern" ]; then
        hits=$(git grep -l "$pattern" -- ':!:tests/test_launcher_leak_grep.py' ':!:scripts/check-install.sh' ':!:docs/REPO_CLEANLINESS.md' ':!:pyproject.toml' ':!:claude_mcp_servers/pyproject.toml' ':!:scripts/check-pre-tag-privacy.sh' 2>/dev/null | grep -Ev "$exclude_pattern" || true)
    else
        hits=$(git grep -l "$pattern" -- ':!:tests/test_launcher_leak_grep.py' ':!:scripts/check-install.sh' ':!:docs/REPO_CLEANLINESS.md' ':!:pyproject.toml' ':!:claude_mcp_servers/pyproject.toml' ':!:scripts/check-pre-tag-privacy.sh' 2>/dev/null || true)
    fi
    if [ -n "$hits" ]; then
        echo "::error::pre-tag privacy gate: $description"
        echo "$hits" | sed 's/^/  - /'
        FAIL=1
    fi
}

echo "[Gate 21] Pre-tag privacy check…"

check_pattern "FABIO-LOCAL block found (operational private state must not ship)" "FABIO-LOCAL"
check_pattern "commercial_workflow/ private path reference found" "commercial_workflow/"
check_pattern "Ombromanto hostname leak" "Ombromanto"
check_pattern "martino-X670E hostname leak" "martino-X670E"
check_pattern "Desktop/PROGETTI/Claude/ author-machine path leak" "Desktop/PROGETTI/Claude"

# Check for HANDOFF-*.md at repo root (gitignore should catch staged ones but verify tracked too)
handoff_hits=$(git ls-tree -r --name-only HEAD | grep -E '^HANDOFF-[^/]+\.md$' || true)
if [ -n "$handoff_hits" ]; then
    echo "::error::pre-tag privacy gate: HANDOFF-*.md files tracked at repo root"
    echo "$handoff_hits" | sed 's/^/  - /'
    FAIL=1
fi

# Check for /home/martino outside intentional sentinels
home_martino_hits=$(git grep -l "/home/martino" -- ':!:tests/test_launcher_leak_grep.py' ':!:scripts/check-install.sh' ':!:docs/REPO_CLEANLINESS.md' ':!:pyproject.toml' ':!:claude_mcp_servers/pyproject.toml' ':!:scripts/check-pre-tag-privacy.sh' ':!:launcher/src-tauri/.cargo/config.toml' ':!:launcher/src-tauri/Cargo.toml' 2>/dev/null || true)
if [ -n "$home_martino_hits" ]; then
    echo "::error::pre-tag privacy gate: /home/martino references found outside intentional sentinels"
    echo "$home_martino_hits" | sed 's/^/  - /'
    FAIL=1
fi

if [ $FAIL -eq 0 ]; then
    echo "[Gate 21] PASS"
else
    echo "[Gate 21] FAIL — fix privacy leaks before tagging"
    exit 1
fi
