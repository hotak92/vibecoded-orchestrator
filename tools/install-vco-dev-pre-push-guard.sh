#!/usr/bin/env bash
# tools/install-vco-dev-pre-push-guard.sh
#
# Installs a pre-push hook that BLOCKS pushes to the public-repo remote
# from a PRIVATE OPERATIONAL VCO checkout (e.g. VCO_dev clones).
#
# Run this once after cloning into a private operational tree.
# It is a no-op if you're in the public-repo clone (the hook will
# never fire because there's no `vco_upstream` / `public` remote
# pointing at the public repo).
#
# Usage:
#   cd <your-private-vco-checkout>
#   bash tools/install-vco-dev-pre-push-guard.sh
#
# The hook checks the destination URL of every push and refuses any
# remote whose URL contains "vibecoded-orchestrator" — i.e. the public
# repo. Pushes to your private fork (origin = hotak92/VCO_dev or
# similar) are allowed.
#
# To bypass (rare — coordinated incident recovery only):
#   git push --no-verify <remote> <ref>

set -euo pipefail

# Locate the actual .git directory (handles worktrees)
GIT_DIR="$(git rev-parse --git-common-dir 2>/dev/null || git rev-parse --git-dir)"

if [ -z "$GIT_DIR" ] || [ ! -d "$GIT_DIR" ]; then
    echo "Error: not inside a git repository, or .git directory not found"
    exit 1
fi

HOOK_PATH="$GIT_DIR/hooks/pre-push"

# Backup any existing pre-push hook
if [ -f "$HOOK_PATH" ] && [ ! -L "$HOOK_PATH" ]; then
    backup="$HOOK_PATH.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    cp "$HOOK_PATH" "$backup"
    echo "Existing pre-push hook backed up to $backup"
fi

cat > "$HOOK_PATH" <<'HOOK_EOF'
#!/usr/bin/env bash
# VCO pre-push guard — refuses pushes to public-repo remote.
#
# This hook is layer 2 of 3 defenses:
#   1. Disable the push URL on any public-repo remote (.git/config)
#   2. THIS hook (pattern-blocks vibecoded-orchestrator destinations)
#   3. This file is installed by tools/install-vco-dev-pre-push-guard.sh
#      (re-installable on fresh clones).
#
# Override (USE WITH CARE): git push --no-verify <remote> <ref>

remote_name="${1:-}"
remote_url="${2:-}"

# Block by name
case "$remote_name" in
  vco_upstream|public|upstream)
    echo "🚫 pre-push: refusing to push to remote '$remote_name' from a VCO operational checkout."
    echo "   Public-repo work goes in a SEPARATE clone of the public repo."
    echo "   This checkout's role is private operational state, not public-source work."
    echo ""
    echo "   If you ABSOLUTELY need to bypass: git push --no-verify $remote_name <ref>"
    exit 1
    ;;
esac

# Block by URL pattern (catches re-added or renamed remotes)
case "$remote_url" in
  *vibecoded-orchestrator*|*vibecoded-tools*)
    echo "🚫 pre-push: refusing to push to '$remote_url' from this VCO operational checkout."
    echo "   URL pattern matches public-repo target."
    exit 1
    ;;
esac

# Default: allow other remotes (origin = private fork, etc.)
exit 0
HOOK_EOF

chmod +x "$HOOK_PATH"
echo "✓ pre-push hook installed at $HOOK_PATH"
echo "✓ Verify with: git push --dry-run vco_upstream HEAD (should be blocked if vco_upstream exists)"
