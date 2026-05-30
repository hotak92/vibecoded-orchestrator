#!/usr/bin/env bash
# Deploy the rl-latest-weights Supabase edge function to production.
#
# Prerequisite (one-time, see README first-time-setup section):
#   - `supabase login` (Supabase CLI auth)
#   - `supabase link --project-ref ovpdtijpdchzlxbojhsg` (from launcher/)
#
# Run this script from the REPO ROOT. It auto-cd's to launcher/ before
# invoking the Supabase CLI because the CLI defaults to
# `./supabase/functions/<name>/index.ts` and our function source lives
# under `launcher/supabase/functions/rl-latest-weights/` — the user's
# 2026-05-30 deploy attempt from the repo root failed with
#   "entrypoint path does not exist (supabase/functions/rl-latest-weights/index.ts)"
# because the CLI looked for `./supabase/...` not `./launcher/supabase/...`.

set -euo pipefail

# Resolve the worktree's repo root so the script works whether invoked
# from the worktree, the deployed clone, or via a symlink. `BASH_SOURCE`
# resolution mirrors `install.py`'s path-discipline pattern.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Script lives at <repo>/launcher/supabase/functions/rl-latest-weights/deploy.sh
# → repo root is 4 levels up.
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
LAUNCHER_DIR="$REPO_ROOT/launcher"

if [ ! -d "$LAUNCHER_DIR/supabase" ]; then
    echo "ERROR: expected supabase project at $LAUNCHER_DIR/supabase — has the repo layout moved?" >&2
    exit 1
fi

if ! command -v supabase >/dev/null 2>&1; then
    echo "ERROR: 'supabase' CLI not found on PATH." >&2
    echo "       Install via https://supabase.com/docs/guides/cli/getting-started" >&2
    exit 1
fi

echo "→ cd $LAUNCHER_DIR"
cd "$LAUNCHER_DIR"

echo "→ supabase functions deploy rl-latest-weights"
supabase functions deploy rl-latest-weights

echo ""
echo "Deploy completed. Smoke test (from README):"
echo "  SUPABASE_URL=https://ovpdtijpdchzlxbojhsg.supabase.co"
echo "  curl -s -X POST \"\$SUPABASE_URL/functions/v1/rl-latest-weights\" \\"
echo "    -H \"Authorization: Bearer <YOUR-PRO-LICENSE>\" \\"
echo "    -H \"Content-Type: application/json\" \\"
echo "    -d '{\"license_key\":\"<YOUR-PRO-LICENSE>\",\"machine_id_hash\":\"$(echo -n test | sha256sum 2>/dev/null | head -c 64)\",\"embedding_source\":\"qwen3\"}' | jq"
