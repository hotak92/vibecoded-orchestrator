#!/usr/bin/env bash
# scripts/check-pre-tag-privacy.sh — Gate 21 of pre-ship-check.
#
# Fails if the tracked tree contains any of the private-state markers
# identified across the v0.2.49 / v0.2.50 leakage audits. Run before
# every tag.
#
# Patterns checked:
#   Track C (operational private state + author-machine layout):
#     - FABIO-LOCAL HTML-comment block (CLAUDE.md operational private state)
#     - commercial_workflow/ path references (private companion repo)
#     - Ombromanto / martino-X670E-Pro-RS hostnames
#     - Desktop/PROGETTI/Claude/ author-machine layout
#     - HANDOFF-*.md at repo root
#     - /home/martino in shipping code outside intentional sentinels
#
#   Track D (other personal-project name leaks):
#     - AI_hive, ARTup, SD15, Agape, FrameAboutYou,
#       MultiagentOrchestrator, SimRacing_AI, MediaLibrary_,
#       Bali_MultiagentOrchestrator, commercial_MAO, DeepTester,
#       InvariantNet, Antigravity
#
# Sentinels that ARE allowed:
#   - tests/test_launcher_leak_grep.py (sentinel inputs for the leak detector)
#   - scripts/check-install.sh (self-test regex)
#   - docs/REPO_CLEANLINESS.md (the policy doc explaining the sentinels)
#   - pyproject.toml maintainer email (AGPL author metadata)
#   - CHANGELOG.md (historical narrative may legitimately name past projects)
#
# This script is additive: when new leak classes are discovered,
# append additional check_pattern calls — do not replace existing
# ones unless the underlying leak has been fully scrubbed AND
# verified not to recur.

set -uo pipefail

FAIL=0

check_pattern() {
    local description="$1"
    local pattern="$2"
    local exclude_pattern="${3:-}"
    local hits
    if [ -n "$exclude_pattern" ]; then
        hits=$(git grep -l -E "$pattern" -- ':!:tests/test_launcher_leak_grep.py' ':!:scripts/check-install.sh' ':!:docs/REPO_CLEANLINESS.md' ':!:pyproject.toml' ':!:claude_mcp_servers/pyproject.toml' ':!:scripts/check-pre-tag-privacy.sh' ':!:scripts/pre-ship-check.sh' ':!:CHANGELOG.md' 2>/dev/null | grep -Ev "$exclude_pattern" || true)
    else
        hits=$(git grep -l -E "$pattern" -- ':!:tests/test_launcher_leak_grep.py' ':!:scripts/check-install.sh' ':!:docs/REPO_CLEANLINESS.md' ':!:pyproject.toml' ':!:claude_mcp_servers/pyproject.toml' ':!:scripts/check-pre-tag-privacy.sh' ':!:scripts/pre-ship-check.sh' ':!:CHANGELOG.md' 2>/dev/null || true)
    fi
    if [ -n "$hits" ]; then
        echo "::error::pre-tag privacy gate: $description"
        echo "$hits" | sed 's/^/  - /'
        FAIL=1
    fi
}

echo "[Gate 21] Pre-tag privacy check…"

# ─────────────────────────────────────────────────────────────────────
# Track C: operational private state + author-machine layout
# ─────────────────────────────────────────────────────────────────────
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
home_martino_hits=$(git grep -l "/home/martino" -- ':!:tests/test_launcher_leak_grep.py' ':!:scripts/check-install.sh' ':!:docs/REPO_CLEANLINESS.md' ':!:pyproject.toml' ':!:claude_mcp_servers/pyproject.toml' ':!:scripts/check-pre-tag-privacy.sh' ':!:launcher/src-tauri/.cargo/config.toml' ':!:launcher/src-tauri/Cargo.toml' ':!:CHANGELOG.md' 2>/dev/null || true)
if [ -n "$home_martino_hits" ]; then
    echo "::error::pre-tag privacy gate: /home/martino references found outside intentional sentinels"
    echo "$home_martino_hits" | sed 's/^/  - /'
    FAIL=1
fi

# ─────────────────────────────────────────────────────────────────────
# Track D: other personal-project name leaks
# (folder names under the maintainer's local project directory that
# are NOT part of VCO and should never appear in the public repo)
# ─────────────────────────────────────────────────────────────────────
check_pattern "AI_hive personal-project leak" "AI_hive"
check_pattern "ARTup personal-project leak" "\bARTup\b"
check_pattern "SD15 personal-project leak (in code/comments, NOT as model name)" "\bSD15\b"
check_pattern "Agape personal-project leak" "\bAgape\b"
check_pattern "FrameAboutYou personal-project leak" "FrameAboutYou"
check_pattern "MultiagentOrchestrator personal-project leak" "MultiagentOrchestrator"
check_pattern "SimRacing_AI personal-project leak" "SimRacing_AI"
check_pattern "MediaLibrary personal-project leak (when used as project class identifier)" "MediaLibrary_"
check_pattern "Bali_MultiagentOrchestrator personal-project leak" "Bali_MultiagentOrchestrator"
check_pattern "commercial_MAO personal-project leak" "commercial_MAO"
check_pattern "DeepTester personal-project leak (when not the testing tool)" "DeepTester"
check_pattern "InvariantNet personal-project leak" "InvariantNet"
check_pattern "Antigravity personal-project leak" "\bAntigravity\b"

# Note: Langflow, OneTrainer, and ARC-AGI are NOT in this gate
# because they are also legitimate public OSS projects/benchmarks.
# If a future audit finds a leak of these names referring to the
# maintainer's local fork (rather than the public OSS project),
# add a more specific pattern targeting the leak context.

# ─────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────
if [ $FAIL -eq 0 ]; then
    echo "[Gate 21] PASS"
else
    echo "[Gate 21] FAIL — fix privacy leaks before tagging"
    exit 1
fi
