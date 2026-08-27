#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# post-mcp-retrieval-record.sh — P4 (v0.2.91)
# PostToolUse on the weaviate-kg RETRIEVAL tools. Records what an EXPLICIT
# retrieval already put in the model's context, into the SAME per-session stores
# the injecting hooks consult, so they stop re-injecting it.
#
# The gap this closes (2026-08-27 perf investigation, §2)
# -------------------------------------------------------
# The session already had two suppression channels — the inject-dedup store
# (things the HOOKS injected) and the explicit-Read ledger (files the model
# Read). Nodes and entities returned by a DELIBERATE `hybrid_search` /
# `semantic_graph_search` / `search_code_graph` call were recorded NOWHERE, so a
# node an agent had just fetched on purpose could be re-injected minutes later by
# the pre-edit hook. That is the single most annoying redundancy class: the model
# is shown something it explicitly went and got.
#
# THE SAFETY RULE: suppress ONLY what is PROVABLY already in context
# ------------------------------------------------------------------
# This hook never records "the model saw this node" — it records "the model has
# these exact bytes". Concretely:
#
#   * KG results → the injector's OWN per-chunk key, "<title>#<sha1(body)[:12]>",
#     computed from the SAME body text `rl_kg_search.py --hook-format` would have
#     printed for that entry at that tier. If the hook later retrieves the same
#     node at a DIFFERENT tier (different body), the hash differs and the block
#     still injects — which is correct: that is new content.
#   * KG results carrying `coverage == "complete"` (the formatter's explicit
#     "all chunks returned" marker) ALSO write the node's source path into the
#     reads-ledger, so any chunk of that node is suppressed. Sound because the
#     WHOLE node is demonstrably in context. A partial view never does this.
#   * Code results → the entity's `full_name`, and ONLY when the result carried
#     `function_body` / `class_body` (the untruncated top tier). A metadata-only
#     "ref" entry records nothing: the model saw a name, not the code.
#
# Everything else is skipped. A `titles`-detail search, a truncated entry, a
# nested connected-node stub — none of them record anything.
#
# Never blocks, never prints to stdout (PostToolUse output would land in the
# transcript). Soft-fail everywhere: a parse failure records nothing.
#
# ONE HOME (CLAUDE.md "share, don't mirror, cross-language logic", option A):
# ALL parsing + key derivation lives in the SHARED
# templates/scripts/mcp_retrieval_record.py, which this hook and its .ps1
# sibling both invoke. Neither shell re-implements the hash-and-parse logic, so
# the two OSes cannot drift into suppressing different things. Both hooks are
# thin argv/stdin shims over that one file.

unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0
# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"
# shellcheck source=_lib/seen-store.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/seen-store.sh" ] && . "$SCRIPT_DIR/_lib/seen-store.sh"

HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -n "$HOOK_STDIN" ] || exit 0

# Untrustworthy session id ("" / "default") → EMPTY store paths → record
# nothing, exactly like the injectors' inject-blind policy. Never compose a
# shared bucket that could bleed one chat's suppression into another.
SESSION_ID_RAW=""
if command -v vco_hook_session_id >/dev/null 2>&1; then
    SESSION_ID_RAW="$(vco_hook_session_id "$HOOK_STDIN")"
fi
[ -n "$SESSION_ID_RAW" ] || exit 0
[ "$SESSION_ID_RAW" = "default" ] && exit 0

command -v vco_seen_store_path >/dev/null 2>&1 || exit 0
INJECT_FILE="$(vco_seen_store_path inject "$SESSION_ID_RAW" "$PROJECT_ROOT")"
READS_FILE="$(vco_seen_store_path reads "$SESSION_ID_RAW" "$PROJECT_ROOT")"
[ -n "$INJECT_FILE" ] || exit 0
mkdir -p "$PROJECT_ROOT/.claude/state" 2>/dev/null || true

# ONE python invocation does the whole parse+append (this is a PostToolUse hook
# on a frequently-used tool; a second interpreter start would be pure tax).
# The parser is the SHARED script (one home for the key derivation — the .ps1
# sibling calls the SAME file, so the two OSes cannot drift into suppressing
# different things). Absent script (partial install) -> silent no-op.
RECORDER="$PROJECT_ROOT/.claude/scripts/mcp_retrieval_record.py"
if [ ! -f "$RECORDER" ]; then
    # Orchestrator-clone layout (templates/ not yet materialized into .claude/).
    if [ -f "$SCRIPT_DIR/../scripts/mcp_retrieval_record.py" ]; then
        RECORDER="$SCRIPT_DIR/../scripts/mcp_retrieval_record.py"
    else
        exit 0
    fi
fi

printf '%s' "$HOOK_STDIN" | "$PY" "$RECORDER" "$INJECT_FILE" "$READS_FILE" 2>/dev/null || true

exit 0
