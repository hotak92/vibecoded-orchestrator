#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# HK-3 (v0.2.73): named-script extraction of the former INLINE knowledge-sync
# hook that lived in settings.json.{linux,windows}.template as a raw
# `python3 -c "..."` / `powershell -Command "..."` command block.
#
# WHY extract (D-5/D-6 acceptance):
#   (a) GUARD — the inline block had no settings-level VCT_DISABLE_HOOKS net
#       on Windows (bash-ism guard, inert under cmd.exe — see D-4) and no
#       internal guard at all; this script self-guards.
#   (b) SCRUB — the inline block spawned `python sync_knowledge_graph.py`
#       with UNSCRUBBED env (secrets-scrub contract evaded). This script
#       scrubs via the canonical _lib/scrub-env.sh list first.
#   (c) ACCURATE ERRORS — the inline block hid every failure behind
#       `|| true` / `catch {}`; a broken venv-resolution (the script imports
#       weaviate/yaml) failed silently forever. This routes through the
#       venv-resolving `kg-sync` wrapper (the one home for that resolution),
#       and emits a single diagnostic line to stderr on real failure.
#   (d) DELETE REDUNDANT INLINE SYNC — post-file-edit.sh §1 ALREADY
#       debounce-syncs knowledge/**/*.md via the same `kg-sync` wrapper, so
#       the inline registration was a redundant, un-debounced second write.
#       The settings templates drop that registration; this script is the
#       single, correct surface if a caller still wants a direct sync.
#
# Contract: reads the hook JSON payload on stdin, extracts
# tool_input.file_path, and — when it points at a knowledge/**/*.md file —
# routes it through the venv-resolving kg-sync wrapper. Soft-fail always
# (exit 0); a missing payload / missing wrapper / non-knowledge path is a
# clean no-op. MUST MATCH kg-sync-on-edit.ps1.

# (b) Scrub sensitive env BEFORE spawning any subprocess (canonical HK-2
# list — MUST MATCH _lib/scrub-env.sh; enforced by the scrub parity gate).
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# (a) Guard: honour the per-shell / per-project disable switch.
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Read the untrusted JSON payload from stdin (never from a TTY).
if [ -t 0 ]; then
    exit 0
fi
INPUT="$(cat 2>/dev/null || true)"
[ -z "$INPUT" ] && exit 0

# Resolve a Python interpreter portably (python3 → python → py) to parse the
# payload — the JSON extraction stays in Python (robust to metacharacters in
# file paths) but the SYNC itself goes through the kg-sync wrapper.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # no Python → silent no-op

# Extract file_path via env (untrusted payload never touches argv/shell).
FILE_PATH="$(KG_SYNC_INPUT="$INPUT" "$PY" - <<'PYEOF' 2>/dev/null || true
import json, os, sys
raw = os.environ.get("KG_SYNC_INPUT", "")
try:
    d = json.loads(raw)
except (ValueError, TypeError):
    sys.exit(0)
ti = d.get("tool_input") or {}
for k in ("file_path", "path", "filePath"):
    v = ti.get(k)
    if v:
        print(v)
        break
PYEOF
)"
[ -z "$FILE_PATH" ] && exit 0

# Only sync knowledge/**/*.md — the development-collection + code-graph paths
# are handled by post-file-edit.sh, not here.
_norm="${FILE_PATH//\\//}"
case "$_norm" in
    */knowledge/*.md|knowledge/*.md) : ;;
    *) exit 0 ;;
esac

# Route through the venv-resolving kg-sync wrapper (the ONE home for the
# weaviate/yaml import-resolution). Prefer the project-local wrapper; fall
# back to the templates copy if the project hasn't been bundled yet.
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
KG_SYNC="$PROJECT_DIR/.claude/scripts/kg-sync"
[ -x "$KG_SYNC" ] || KG_SYNC=""

if [ -n "$KG_SYNC" ]; then
    if ! "$KG_SYNC" "$FILE_PATH" >/dev/null 2>&1; then
        # (c) Accurate, single-line diagnostic — no silent `|| true` swallow.
        printf '[kg-sync-on-edit] kg-sync failed for %s (KG may be stale)\n' \
            "$FILE_PATH" >&2
    fi
fi

exit 0
