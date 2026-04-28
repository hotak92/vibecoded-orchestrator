#!/usr/bin/env bash
# install-githooks.sh — Activate repo-tracked git hooks under .githooks/.
#
# Run once per clone:
#   bash scripts/install-githooks.sh
#
# Sets git's core.hooksPath to .githooks/ so the repo-tracked hooks fire
# instead of the per-clone ones in .git/hooks/. Idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "$REPO_ROOT/.githooks" ]; then
    echo "[install-githooks] $REPO_ROOT/.githooks does not exist — nothing to install." >&2
    exit 1
fi

# Make every hook executable (git silently skips non-executable hooks).
chmod +x "$REPO_ROOT"/.githooks/* 2>/dev/null || true

cd "$REPO_ROOT"
current="$(git config --get core.hooksPath 2>/dev/null || echo '')"
if [ "$current" = ".githooks" ]; then
    echo "[install-githooks] core.hooksPath is already .githooks — nothing to do."
else
    git config core.hooksPath .githooks
    echo "[install-githooks] core.hooksPath -> .githooks"
fi

echo "[install-githooks] Active hooks:"
ls -1 "$REPO_ROOT/.githooks/" | sed 's/^/  - /'
