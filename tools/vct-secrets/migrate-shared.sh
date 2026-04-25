#!/usr/bin/env bash
# migrate-shared.sh — Move flat ~/.vct-secrets/<key> files into ~/.vct-secrets/shared/<key>.
# Safe: preserves perms (chmod 600), refuses if destination exists, supports --dry-run.
#
# Usage:
#   ./migrate-shared.sh           # perform move
#   ./migrate-shared.sh --dry-run # show what would happen
#
# Only moves files that match the known flat-secret list. Never touches CLI,
# wrappers, audit log, or project dirs.

set -eu
umask 077

ROOT="${VCT_SECRETS_DIR:-$HOME/.vct-secrets}"
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then DRY_RUN=1; fi

# Known flat-shared secrets (per design doc §Migration / Current state inventory).
KNOWN_FLAT=(
    claude_code_oauth_token
    github_pat
    huggingface_token
    squeezylemon_api_token
    supabase_private_key
    supabase_publishable_key
    supabase_token
    telegram_onboarding_chat_id
    vercel_token
)

mkdir -p "$ROOT/shared"
chmod 700 "$ROOT/shared"

moved=0
skipped=0
for key in "${KNOWN_FLAT[@]}"; do
    src="$ROOT/$key"
    dst="$ROOT/shared/$key"
    if [ ! -f "$src" ]; then
        printf '  (not present: %s)\n' "$key"
        continue
    fi
    if [ -e "$dst" ]; then
        printf '  SKIP %s — destination already exists at %s\n' "$key" "$dst" >&2
        skipped=$((skipped+1))
        continue
    fi
    if [ $DRY_RUN -eq 1 ]; then
        printf '  would move: %s → shared/%s\n' "$key" "$key"
    else
        mv -- "$src" "$dst"
        chmod 600 "$dst"
        printf '  moved: %s → shared/%s\n' "$key" "$key"
    fi
    moved=$((moved+1))
done

if [ $DRY_RUN -eq 1 ]; then
    printf '\nDRY RUN: %d files would move, %d would skip. Run without --dry-run to apply.\n' "$moved" "$skipped"
else
    printf '\nDone: %d files moved, %d skipped.\n' "$moved" "$skipped"
    printf 'Verify: vct list\n'
fi
