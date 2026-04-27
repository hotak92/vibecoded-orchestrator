#!/usr/bin/env bash
# Pre-commit guard: refuse to commit known-leaked tokens.
#
# Run from repo root:
#   ./scripts/check-no-secrets.sh
#
# Wire as a git pre-commit hook with:
#   ln -sf ../../scripts/check-no-secrets.sh .git/hooks/pre-commit
#
# The blocklist below is the historical-leak list — values that have
# already been exposed in this repo and that must NEVER reappear, even
# in documentation. Replace any new occurrence with a clear placeholder
# (e.g. `<YOUR_FOO>`) and document how to generate the real value.

set -euo pipefail

# Tokens / project refs that have leaked at some point in this repo's
# history. Treat each as compromised forever.
BLOCKLIST=(
  # Lemon Squeezy webhook signing secret leaked in launcher/docs (commit
  # 2f1cc88, 2026-03-07). Sanitized in oss/round3-secrets-rotation-and-admin.
  "wh_vct_ls_2026_s3cur3k3y"

  # Supabase project ref leaked alongside the webhook secret. The public
  # alias https://api.vibecodedtools.it/* should be used instead.
  "ltnlwhaxnpbiifordlbk"
)

# Files we don't want to scan (binaries, generated, vendored).
EXCLUDE_PATHS=(
  ":(exclude)CHANGELOG.md"
  ":(exclude)scripts/check-no-secrets.sh"
  ":(exclude).git/**"
  ":(exclude)**/node_modules/**"
  ":(exclude)**/target/**"
  ":(exclude)**/.next/**"
  ":(exclude)**/dist/**"
  ":(exclude)**/build/**"
)

# Determine the file set:
# - if invoked as a pre-commit hook → only the staged additions
# - otherwise → the full tracked tree
if [ -n "${GIT_INDEX_FILE:-}" ] || git rev-parse --verify HEAD >/dev/null 2>&1; then
  if [ -n "${1:-}" ] && [ "${1}" = "--staged" ]; then
    file_list=$(git diff --cached --name-only --diff-filter=ACMR)
  elif [ "${1:-}" = "--all" ]; then
    file_list=$(git ls-files -- "${EXCLUDE_PATHS[@]}")
  else
    file_list=$(git ls-files -- "${EXCLUDE_PATHS[@]}")
  fi
else
  echo "check-no-secrets.sh: not in a git repo, scanning current dir tree"
  file_list=$(find . -type f \( -name "*.md" -o -name "*.ts" -o -name "*.py" -o -name "*.rs" -o -name "*.toml" -o -name "*.json" -o -name "*.sh" \) | grep -v node_modules | grep -v target)
fi

violations=0
for token in "${BLOCKLIST[@]}"; do
  # -F = fixed string, -l = filename only
  if matches=$(echo "$file_list" | xargs -r grep -l -F -- "$token" 2>/dev/null); then
    if [ -n "$matches" ]; then
      printf 'BLOCKED: leaked token "%s" found in:\n' "$token" >&2
      while IFS= read -r f; do
        [ -z "$f" ] && continue
        printf '  %s\n' "$f" >&2
      done <<< "$matches"
      violations=$((violations + 1))
    fi
  fi
done

if [ "$violations" -gt 0 ]; then
  echo "" >&2
  echo "Refusing to commit. Replace each occurrence with a placeholder" >&2
  echo "(e.g. <YOUR_LS_WEBHOOK_SIGNING_SECRET>) and document how to" >&2
  echo "generate a real value. See the project secrets rotation runbook." >&2
  exit 1
fi

echo "check-no-secrets: OK (no known-leaked tokens found)"
exit 0
