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

# Shared secret-shape predicate (bash mirror of the Python SSOT). ONE copy,
# sourced by both `vct` and this script (SHARED-CODE RULE — no duplication).
# CDPATH= prefixes the `cd` (neutralises a user CDPATH); not a var assignment.
# shellcheck disable=SC1007
_MS_SELF_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)
# shellcheck source=lib/secret_shape.sh
if [ -r "$_MS_SELF_DIR/lib/secret_shape.sh" ]; then
    . "$_MS_SELF_DIR/lib/secret_shape.sh"
else
    printf 'migrate-shared: error: shared predicate missing at %s/lib/secret_shape.sh — broken install\n' \
        "$_MS_SELF_DIR" >&2
    exit 1
fi

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
    # Write-time shape guard (Part A): never migrate a multi-secret BLOB into the
    # shared store — that would faithfully propagate the corruption. Shape-check
    # the SOURCE via the shared predicate; on a blob, SKIP (leave the source in
    # place, nothing lost), drop an empty `.needs-split` marker beside it, and
    # loudly warn to run `vct recover-blob`. Never read/print the value.
    src_val=$(cat -- "$src"; printf x); src_val=${src_val%x}
    if ! reason=$(_is_single_line_secret "$src_val" "$key" 0); then
        unset src_val
        printf '  SKIP %s — source is a malformed/blob value (reason: %s). Left in place; not migrated. Run: vct recover-blob --shared --key %s\n' \
            "$key" "$reason" "$key" >&2
        # Empty marker file next to the source (chmod 600). Idempotent: skip if present.
        marker="$src.needs-split"
        if [ $DRY_RUN -eq 1 ]; then
            printf '  would mark: %s.needs-split\n' "$key" >&2
        elif [ ! -e "$marker" ]; then
            : > "$marker"
            chmod 600 "$marker" 2>/dev/null || true
        fi
        skipped=$((skipped+1))
        continue
    fi
    unset src_val
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
