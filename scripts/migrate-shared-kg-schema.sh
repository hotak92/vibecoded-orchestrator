#!/usr/bin/env bash
# migrate-shared-kg-schema.sh
#
# Drop + recreate the shared KG collection when its schema lacks
# `invertedIndexConfig.indexNullState=True`. Weaviate <=1.30 cannot
# add `indexNullState` retroactively (verified 2026-04-30, see
# vco_lib/project_init.py::detect_kg_schema_drift); the only fix is a
# destructive recreate.
#
# Safe because the shared KG content derives from knowledge/**/*.md in
# the orchestrator clone — the migration drops the collection, lets the
# next sync pass recreate it with the correct schema, and re-ingests
# from the .md sources.
#
# Idempotent: if indexNullState is already True the script is a no-op.
# If the shared KG doesn't exist yet, the script is a no-op (the seed
# step will create it with the correct schema).
#
# Soft-fail: failure to drop/resync emits a warning + exit 0 so
# install.py --update can convert the failure into a deferral entry.
#
# Env vars:
#   WEAVIATE_URL          — defaults to http://localhost:8081
#   SHARED_KG_COLLECTION  — defaults to VibeCodedOrchestrator_KnowledgeGraph
#                           (capital-C casing since v0.2.23 B1; was
#                           lowercase-c "VibecodedOrchestrator_KnowledgeGraph"
#                           v0.2.12–v0.2.22, itself renamed from
#                           VibeCodedTools_KnowledgeGraph pre-v0.2.12).
#                           Must stay in lockstep with
#                           vco_lib/project_init.py::_SHARED_KG_NAME.
#
# Requires: bash, curl, jq. Optionally invokes
# `.claude/scripts/kg-sync --all` after the drop to repopulate; if that
# script is not on PATH the migration logs a hint and exits, leaving the
# collection empty (the next `.claude/scripts/kg-sync` run will fill it).

set -uo pipefail

WEAVIATE_URL="${WEAVIATE_URL:-http://localhost:8081}"
SHARED_KG="${SHARED_KG_COLLECTION:-VibeCodedOrchestrator_KnowledgeGraph}"

if ! command -v curl >/dev/null 2>&1; then
    echo "[migrate-shared-kg] curl not found; skipping migration." >&2
    exit 0
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "[migrate-shared-kg] jq not found; skipping migration." >&2
    exit 0
fi

if ! curl -fsS "$WEAVIATE_URL/v1/.well-known/ready" >/dev/null 2>&1; then
    echo "[migrate-shared-kg] Weaviate not reachable at $WEAVIATE_URL; skipping." >&2
    exit 0
fi

# Probe current schema. Three outcomes:
#   - HTTP 404 (collection missing): nothing to migrate.
#   - indexNullState=true: schema already correct.
#   - indexNullState=false or absent: needs drop+recreate.
RAW_SCHEMA="$(curl -sS -o /tmp/_migrate_shared_kg_schema.$$ -w '%{http_code}' \
    "$WEAVIATE_URL/v1/schema/$SHARED_KG" 2>/dev/null || echo "000")"

case "$RAW_SCHEMA" in
    200)
        CURRENT="$(jq -r '.invertedIndexConfig.indexNullState // false' \
            < /tmp/_migrate_shared_kg_schema.$$ 2>/dev/null || echo "false")"
        ;;
    404)
        echo "[migrate-shared-kg] Shared KG '$SHARED_KG' does not exist; nothing to migrate."
        rm -f /tmp/_migrate_shared_kg_schema.$$
        exit 0
        ;;
    *)
        echo "[migrate-shared-kg] Unexpected HTTP $RAW_SCHEMA from schema probe; skipping." >&2
        rm -f /tmp/_migrate_shared_kg_schema.$$
        exit 0
        ;;
esac

rm -f /tmp/_migrate_shared_kg_schema.$$

if [ "$CURRENT" = "true" ]; then
    echo "[migrate-shared-kg] $SHARED_KG already has indexNullState=true; no migration needed."
    exit 0
fi

echo "[migrate-shared-kg] $SHARED_KG indexNullState=$CURRENT; dropping + recreating ..."

DROP_HTTP="$(curl -sS -o /dev/null -w '%{http_code}' \
    -X DELETE "$WEAVIATE_URL/v1/schema/$SHARED_KG" 2>/dev/null || echo "000")"

case "$DROP_HTTP" in
    200|204|404)
        echo "[migrate-shared-kg] Drop OK (HTTP $DROP_HTTP)."
        ;;
    *)
        echo "[migrate-shared-kg] Drop failed (HTTP $DROP_HTTP); aborting migration." >&2
        exit 0
        ;;
esac

# Re-ingest from knowledge/**/*.md. The kg-sync helper recreates the
# collection with the canonical schema (project_init.kg_class_definition,
# which sets indexNullState=True) and then writes the .md sources back.
#
# We unset DEVELOPMENT_COLLECTION + SHARED_KG_COLLECTION on the way in so
# kg-sync does NOT also touch the per-project Dev collection during the
# shared-KG-only resync; setting KG_COLLECTION to the shared name routes
# the writes into the shared collection instead of the per-project one.
RESYNC_SCRIPT=""
for CANDIDATE in \
    "$(dirname "$0")/../.claude/scripts/kg-sync" \
    ".claude/scripts/kg-sync"; do
    if [ -x "$CANDIDATE" ]; then
        RESYNC_SCRIPT="$CANDIDATE"
        break
    fi
done

if [ -z "$RESYNC_SCRIPT" ]; then
    echo "[migrate-shared-kg] kg-sync helper not found; collection is dropped but"
    echo "[migrate-shared-kg] not yet repopulated. Run '.claude/scripts/kg-sync --all'"
    echo "[migrate-shared-kg] manually to recreate $SHARED_KG with the correct schema."
    exit 0
fi

echo "[migrate-shared-kg] Resyncing via $RESYNC_SCRIPT ..."
if ! KG_COLLECTION="$SHARED_KG" \
     DEVELOPMENT_COLLECTION="" \
     SHARED_KG_COLLECTION="" \
     "$RESYNC_SCRIPT" --all; then
    echo "[migrate-shared-kg] Resync exited non-zero; the collection may be empty." >&2
    exit 0
fi

VERIFY="$(curl -fsS "$WEAVIATE_URL/v1/schema/$SHARED_KG" 2>/dev/null \
    | jq -r '.invertedIndexConfig.indexNullState // false' 2>/dev/null || echo "unknown")"
echo "[migrate-shared-kg] Done. Post-migration indexNullState=$VERIFY."

exit 0
