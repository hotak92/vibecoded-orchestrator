#!/usr/bin/env bash
# migrate-development-temporal-props.sh
#
# Add the four canonical temporal properties (created, updated, valid_from,
# valid_until) to every existing *_Development, *_KnowledgeGraph, and
# *_Diagrams collection in the running Weaviate. Properties can be added
# retroactively via the v1 schema REST API
# (POST /v1/schema/<class>/properties returns 200 on success, 422 on
# already-present); unlike `invertedIndexConfig.indexNullState` they do NOT
# require a destructive recreate.
#
# Idempotent: per-property presence is checked before POST, so re-running the
# script is a no-op when the schema is already correct.
#
# Soft-fail per collection: if a single POST fails the script logs the error
# and continues with the next collection / property. The script exits 0 even
# if some operations failed, so install.py --update never aborts on a
# partial migration.
#
# Env vars:
#   WEAVIATE_URL  — defaults to http://localhost:8081
#
# Requires: bash, curl, jq (skipped with a clear message if jq is missing).
#
# Coordinated with: scripts/migrate-shared-kg-schema.sh (PR-24, 2026-05-16).
#
# V52-I Fix B (2026-06-09): regex extended from `_Development$` to
# `(_KnowledgeGraph|_Development|_Diagrams)$` so existing shared KG and
# diagrams collections gain the temporal date props on the next
# `install.py --update`. Closes the gap that produced 30 false-positive
# `partial_fan_out_schema_missing` MCP telemetry events. The companion
# Fix A in `claude_mcp_servers/weaviate_mcp/server.py` is the runtime
# defensive layer; this script is the permanent schema closure.

set -uo pipefail

WEAVIATE_URL="${WEAVIATE_URL:-http://localhost:8081}"

if ! command -v curl >/dev/null 2>&1; then
    echo "[migrate-dev-props] curl not found on PATH; skipping migration." >&2
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "[migrate-dev-props] jq not found on PATH; skipping migration." >&2
    echo "[migrate-dev-props] Install jq (e.g. 'apt install jq') and re-run install.py --update." >&2
    exit 0
fi

# Verify Weaviate is reachable before doing anything.
if ! curl -fsS "$WEAVIATE_URL/v1/.well-known/ready" >/dev/null 2>&1; then
    echo "[migrate-dev-props] Weaviate not reachable at $WEAVIATE_URL; skipping." >&2
    exit 0
fi

SCHEMA_JSON="$(curl -fsS "$WEAVIATE_URL/v1/schema" 2>/dev/null || true)"
if [ -z "$SCHEMA_JSON" ]; then
    echo "[migrate-dev-props] Failed to read schema from $WEAVIATE_URL/v1/schema; skipping." >&2
    exit 0
fi

# Discover all _Development, _KnowledgeGraph, and _Diagrams collections
# in one pass. V52-I Fix B (2026-06-09): the original regex was
# `_Development$` only — extending to the three-suffix alternation closes
# the gap that produced 30 false-positive partial_fan_out_schema_missing
# MCP telemetry events on shared KG + diagram collections.
COLLECTIONS="$(echo "$SCHEMA_JSON" | jq -r '.classes[]?.class | select(test("(_KnowledgeGraph|_Development|_Diagrams)$"))' 2>/dev/null || true)"

if [ -z "$COLLECTIONS" ]; then
    echo "[migrate-dev-props] No *_Development / *_KnowledgeGraph / *_Diagrams collections found; nothing to migrate."
    exit 0
fi

ANY_CHANGE=0
ANY_ERROR=0

for COLL in $COLLECTIONS; do
    COLL_SCHEMA="$(curl -fsS "$WEAVIATE_URL/v1/schema/$COLL" 2>/dev/null || true)"
    if [ -z "$COLL_SCHEMA" ]; then
        echo "[migrate-dev-props] $COLL: failed to fetch schema; skipping." >&2
        ANY_ERROR=1
        continue
    fi
    EXISTING_PROPS="$(echo "$COLL_SCHEMA" | jq -r '.properties[]?.name' 2>/dev/null || true)"

    for PROP in created updated valid_from valid_until; do
        if printf '%s\n' "$EXISTING_PROPS" | grep -qx "$PROP"; then
            echo "[migrate-dev-props] $COLL.$PROP already present; skip."
            continue
        fi
        echo "[migrate-dev-props] $COLL.$PROP missing; adding ..."
        RESP="$(curl -sS -o /dev/null -w '%{http_code}' \
            -X POST "$WEAVIATE_URL/v1/schema/$COLL/properties" \
            -H 'Content-Type: application/json' \
            -d "{\"name\":\"$PROP\",\"dataType\":[\"date\"]}" 2>/dev/null || echo "000")"
        case "$RESP" in
            200|201|204)
                echo "[migrate-dev-props] $COLL.$PROP added (HTTP $RESP)."
                ANY_CHANGE=1
                ;;
            422)
                # Already present (concurrent migration or race with sync).
                echo "[migrate-dev-props] $COLL.$PROP returned 422 — already present; skip."
                ;;
            *)
                echo "[migrate-dev-props] $COLL.$PROP add failed (HTTP $RESP); continuing." >&2
                ANY_ERROR=1
                ;;
        esac
    done
done

if [ "$ANY_CHANGE" = "1" ]; then
    echo "[migrate-dev-props] Done (changes applied)."
elif [ "$ANY_ERROR" = "1" ]; then
    echo "[migrate-dev-props] Done (some operations failed; see log)." >&2
else
    echo "[migrate-dev-props] Done (no changes needed)."
fi

# Always exit 0 — install.py treats a failing migration as a deferral, not
# a fatal error.
exit 0
