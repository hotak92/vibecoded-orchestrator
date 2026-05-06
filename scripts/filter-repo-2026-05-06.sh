#!/usr/bin/env bash
# scripts/filter-repo-2026-05-06.sh
#
# History-rewrite script for PR-4 (privacy + cleanliness).
#
# Purpose: purge two classes of history-only artifacts that the
# 2026-05-06 cleanliness audit (.claude/context/repo-cleanliness-
# audit-2026-05-06.md in the orchestrator host repo) flagged:
#
#   §2.a — REAL privacy leak. `launcher/.mcp.json` and `.mcp.json`,
#          added in launcher subtree squash, deleted in HEAD by
#          commit 9775a14 ("pre-public scrub"). The squash + add
#          commits are still reachable in history. Leaks Fabio's
#          Windows username `fabio`, his C:\Users\fabio layout,
#          and personal Weaviate collection names.
#
#   §2.c — Internal docs that were deliberately removed from HEAD
#          but contain admin-user identities, internal procedures,
#          and process notes that aren't useful in public history.
#          No exploitable secrets — just embarrassment-class
#          reputation/process exposure.
#
# This script does NOT execute the rewrite by itself. It runs the
# rewrite in a FRESH bare clone under /tmp/, verifies the result,
# and prints the manual `git push --force-with-lease` command for
# the operator to execute (with team coordination — force-pushing
# main requires ALL collaborators to re-clone, NOT pull, after).
#
# ──────────────────────────────────────────────────────────────
# Pre-requisites:
#   - git-filter-repo installed:
#       pip install --user git-filter-repo
#     (NOT to be confused with `git filter-branch` which is
#     deprecated; filter-repo is faster and the upstream
#     recommendation since 2022.)
#   - gh CLI logged in (only needed if the operator wants to
#     verify the post-push tip via the API — not required for
#     the rewrite itself).
#
# ──────────────────────────────────────────────────────────────
# Usage:
#   bash scripts/filter-repo-2026-05-06.sh                 # dry run (default)
#   bash scripts/filter-repo-2026-05-06.sh --rewrite       # actually rewrite into /tmp/vco-rewrite
#
# Even with --rewrite, the script does NOT push. The final
# `git push --force-with-lease origin main` is the operator's
# manual step — done DELIBERATELY so the operator confirms team
# coordination is in place before the irreversible push.
#
# ──────────────────────────────────────────────────────────────
# Safety:
#   - Operates on a SEPARATE bare clone at /tmp/vco-rewrite.
#   - Does NOT touch the dev's working clone.
#   - Refuses to push.
#   - Prints a verification step the operator must read before
#     pushing.
#
# ──────────────────────────────────────────────────────────────
set -euo pipefail

REPO_URL="https://github.com/hotak92/vibecoded-orchestrator.git"
WORK_DIR="/tmp/vco-rewrite"
MODE="${1:-dry-run}"

# Paths to purge from ALL history. Cross-checked 2026-05-06
# against `git log --all --diff-filter=D --name-only` filtered
# for the leak-classes listed in the audit.
PATHS_TO_PURGE=(
    # §2.a — real privacy leak
    .mcp.json
    launcher/.mcp.json

    # §2.c — internal docs (deleted in HEAD by 9775a14, 998275d)
    docs/ADMIN_LICENSE.md
    docs/HANDOFF-MARTINO.md
    docs/SECRETS_ROTATION.md
    docs/SECURITY_PORTING_GUIDE.md
    docs/license/VARIANT_SETUP.md
    docs/POSITIONING.md
    docs/USER_JOURNEY.md
    launcher/docs/HANDOFF-MARTINO.md
    OSS_LAUNCH_READINESS.md
)

print_paths() {
    echo "Paths queued for filter-repo --invert-paths removal:"
    for p in "${PATHS_TO_PURGE[@]}"; do
        echo "  - $p"
    done
}

require_filter_repo() {
    if ! command -v git-filter-repo >/dev/null 2>&1; then
        echo "ERROR: git-filter-repo not on PATH." >&2
        echo "Install with: pip install --user git-filter-repo" >&2
        echo "Then re-run this script." >&2
        exit 1
    fi
}

dry_run() {
    print_paths
    echo
    echo "Dry-run only. Re-run with --rewrite to perform the rewrite"
    echo "in a fresh bare clone at $WORK_DIR (does NOT touch your"
    echo "current working clone)."
    echo
    echo "After --rewrite finishes, the operator must MANUALLY run:"
    echo "  cd $WORK_DIR"
    echo "  git push --force-with-lease origin main"
    echo "(Not run by this script — see Safety section above.)"
}

rewrite() {
    require_filter_repo
    print_paths
    echo

    if [ -d "$WORK_DIR" ]; then
        echo "ERROR: $WORK_DIR already exists. Remove it first:" >&2
        echo "  rm -rf $WORK_DIR" >&2
        exit 1
    fi

    echo "Step 1/4: cloning $REPO_URL into $WORK_DIR (bare)..."
    git clone --bare "$REPO_URL" "$WORK_DIR"
    cd "$WORK_DIR"

    echo
    echo "Step 2/4: running git-filter-repo --invert-paths..."
    # Build the --path argument list. filter-repo accepts multiple
    # --path entries; --invert-paths flips it to a delete list.
    args=(--invert-paths --force)
    for p in "${PATHS_TO_PURGE[@]}"; do
        args+=(--path "$p")
    done
    git filter-repo "${args[@]}"

    echo
    echo "Step 3/4: verifying purge..."
    # Verification: any commit that touched a purged path should
    # no longer be reachable via `git log --all --name-only`.
    local missing_after=0
    for p in "${PATHS_TO_PURGE[@]}"; do
        # `grep -F -x -q -- "$p"` — fixed-string, full-line match,
        # quiet (exit code only). The `--` ends option parsing so a
        # path that starts with `-` doesn't get treated as an option.
        if git log --all --pretty=format: --name-only 2>/dev/null \
                | grep -F -x -q -- "$p"; then
            echo "  STILL PRESENT: $p" >&2
            missing_after=1
        else
            echo "  purged: $p"
        fi
    done
    if [ "$missing_after" -ne 0 ]; then
        echo "ERROR: filter-repo did not purge all listed paths." >&2
        echo "Inspect $WORK_DIR by hand before pushing." >&2
        exit 2
    fi

    echo
    echo "Step 4/4: rewrite complete."
    echo
    echo "═══════════════════════════════════════════════════════"
    echo "  MANUAL STEPS (operator):"
    echo "═══════════════════════════════════════════════════════"
    echo
    echo "1. Verify the rewrite locally:"
    echo "     cd $WORK_DIR"
    echo "     git log --all --pretty=format: --name-only \\"
    echo "       | grep -E '\\.mcp\\.json$' || echo 'CLEAN'"
    echo
    echo "2. Coordinate with collaborators BEFORE pushing. Force-"
    echo "   pushing main requires every existing clone to be"
    echo "   RE-CLONED, not 'git pull'-ed. Send a heads-up to"
    echo "   the team channel and confirm receipt before step 3."
    echo
    echo "3. Push (this is the irreversible step):"
    echo "     cd $WORK_DIR"
    echo "     git push --force-with-lease origin main"
    echo "     # If you have other long-lived branches, push them"
    echo "     # too. List with: git branch -a"
    echo
    echo "4. After push, GitHub recommends running the GC API to"
    echo "   make the purged blobs unreachable from the web UI"
    echo "   sooner (otherwise they linger ~30 days):"
    echo "     gh api -X POST /repos/hotak92/vibecoded-orchestrator/dispatches \\"
    echo "       -f event_type=run-gc"
    echo "   (No-op if the repo doesn't have a 'run-gc' workflow"
    echo "   wired up. Filing an issue with GitHub Support is the"
    echo "   alternative for accelerated GC.)"
    echo
    echo "5. Notify the team to re-clone + delete their old clones."
    echo "═══════════════════════════════════════════════════════"
}

main() {
    case "$MODE" in
        dry-run|--dry-run|"")
            dry_run
            ;;
        --rewrite|rewrite)
            rewrite
            ;;
        -h|--help|help)
            sed -n '1,/^set -euo pipefail$/p' "$0"
            ;;
        *)
            echo "Unknown mode: $MODE" >&2
            echo "Usage: $0 [--dry-run|--rewrite|--help]" >&2
            exit 1
            ;;
    esac
}

main
