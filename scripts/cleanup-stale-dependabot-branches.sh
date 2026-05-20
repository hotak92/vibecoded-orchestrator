#!/usr/bin/env bash
# cleanup-stale-dependabot-branches.sh
#
# One-time prune of stale Dependabot branches that no longer have an open PR.
#
# SAFETY:
#   - Only touches branches matching the literal prefix `dependabot/`.
#     Branches from human authors (hotak92, pb992, feature branches,
#     release branches, etc.) are never enumerated.
#   - Defaults to DRY-RUN. Pass `--apply` to actually delete.
#   - Requires `gh` authenticated against the target repo.
#
# Usage:
#   bash scripts/cleanup-stale-dependabot-branches.sh                       # dry-run, current repo
#   bash scripts/cleanup-stale-dependabot-branches.sh --apply               # delete, current repo
#   bash scripts/cleanup-stale-dependabot-branches.sh --repo OWNER/NAME     # target a specific repo
#   bash scripts/cleanup-stale-dependabot-branches.sh --repo OWNER/NAME --apply

set -euo pipefail

REPO=""
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --repo)  REPO="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

PR_REPO_ARG=()
[[ -n "$REPO" ]] && PR_REPO_ARG=(--repo "$REPO")

# `gh api` doesn't accept --repo; resolve the owner/name and embed it.
if [[ -n "$REPO" ]]; then
  API_REPO="$REPO"
else
  API_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
fi

echo "Mode: $([[ $APPLY -eq 1 ]] && echo APPLY || echo DRY-RUN)"
echo "Repo: $API_REPO"
echo

# Open Dependabot PR head refs — these MUST be preserved.
mapfile -t OPEN_PRS < <(
  gh pr list "${PR_REPO_ARG[@]}" \
    --author "dependabot[bot]" --state open \
    --json headRefName --jq '.[].headRefName'
)
echo "Open Dependabot PRs: ${#OPEN_PRS[@]} branches protected"

# All remote branches with the dependabot/ prefix.
mapfile -t DEPBOT_BRANCHES < <(
  gh api "repos/$API_REPO/branches" --paginate \
    --jq '.[].name' | grep '^dependabot/' || true
)
echo "Total dependabot/* branches: ${#DEPBOT_BRANCHES[@]}"
echo

stale=()
for b in "${DEPBOT_BRANCHES[@]}"; do
  protected=0
  for p in "${OPEN_PRS[@]}"; do
    [[ "$b" == "$p" ]] && { protected=1; break; }
  done
  [[ $protected -eq 0 ]] && stale+=("$b")
done

echo "Stale branches (no open PR): ${#stale[@]}"
for b in "${stale[@]}"; do echo "  - $b"; done

[[ ${#stale[@]} -eq 0 ]] && { echo "Nothing to do."; exit 0; }

if [[ $APPLY -eq 0 ]]; then
  echo
  echo "DRY-RUN: re-run with --apply to delete the ${#stale[@]} branches above."
  exit 0
fi

echo
echo "Deleting ${#stale[@]} branches..."
for b in "${stale[@]}"; do
  if gh api -X DELETE "repos/$API_REPO/git/refs/heads/$b" >/dev/null 2>&1; then
    echo "  deleted: $b"
  else
    echo "  FAILED:  $b" >&2
  fi
done

echo "Done."
