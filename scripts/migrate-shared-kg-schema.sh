#!/usr/bin/env bash
# migrate-shared-kg-schema.sh
#
# Drop + recreate the shared KG collection when its schema lacks
# `invertedIndexConfig.indexNullState=True`. Weaviate <=1.30 cannot
# add `indexNullState` retroactively (verified 2026-04-30, see
# vco_lib/project_init.py::detect_kg_schema_drift); the only fix is a
# destructive recreate.
#
# DATA-SAFETY CONTRACT (v0.2.54 Track D / audit P0-2):
#
# The pre-v0.2.54 header claimed the drop was "safe because the shared
# KG content derives from knowledge/**/*.md in the orchestrator clone".
# That premise is FALSE for multi-project installs:
# `store_knowledge_node(scope="shared")` from any OTHER project writes
# its .md under that project's own knowledge/ tree and stores a
# project-relative `file_path` — those sources are invisible to the
# orchestrator-clone `kg-sync --all` resync, so drop+resync permanently
# loses every shared node contributed by user projects.
#
# Two guards now run BEFORE the drop:
#
#   1. kg-sync presence: if the resync helper can't be located, the
#      script ABORTS (exit 4) without dropping. Pre-fix it dropped and
#      exited 0 with the collection empty.
#   2. Cross-project shared-write probe: every stored `file_path` is
#      checked against the orchestrator clone root. Any node whose
#      source is NOT restorable from this clone (project-relative path
#      from another project, absolute path, probe failure, or >10000
#      objects — beyond the probe window) → the script REFUSES (exit 3)
#      unless the caller explicitly consents to the loss via
#      `VCO_SHARED_KG_MIGRATE_CONSENT=1`.
#
# Non-zero exits flow back as `schema_migration_failed_shared_kg_schema`
# deferral entries on the install.py path and as ok=false + stderr in
# the launcher's consent modal — both surfaces show the refusal reason
# and the exact consent command.
#
# Idempotent: if indexNullState is already True the script is a no-op.
# If the shared KG doesn't exist yet, the script is a no-op (the seed
# step will create it with the correct schema).
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
#   VCO_SHARED_KG_MIGRATE_CONSENT — set to 1 to accept the loss of
#                           cross-project shared nodes and proceed with
#                           the drop anyway.
#
# Exit codes:
#   0 — no-op (already migrated / collection absent / tooling or
#       Weaviate missing) OR migration completed.
#   3 — refused: unrecoverable cross-project shared nodes detected (or
#       the safety probe could not verify) and no consent given.
#   4 — refused: kg-sync resync helper not found; dropping would leave
#       the collection empty with no repopulation path.
#
# Requires: bash, curl, jq.

set -uo pipefail

WEAVIATE_URL="${WEAVIATE_URL:-http://localhost:8081}"
SHARED_KG="${SHARED_KG_COLLECTION:-VibeCodedOrchestrator_KnowledgeGraph}"
CONSENT="${VCO_SHARED_KG_MIGRATE_CONSENT:-0}"

# Orchestrator clone root = parent of the scripts/ dir this file lives in.
# Used by the shared-write probe to test whether each stored file_path is
# restorable from this clone's knowledge/ tree.
CLONE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

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

echo "[migrate-shared-kg] $SHARED_KG indexNullState=$CURRENT; migration needed."

# ---------------------------------------------------------------------------
# GUARD 1 (BEFORE drop): locate the kg-sync resync helper. Pre-v0.2.54 this
# lookup ran AFTER the DELETE — a missing helper meant "collection dropped,
# exit 0, nothing repopulates". Now: no helper → no drop.
# ---------------------------------------------------------------------------
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
    echo "[migrate-shared-kg] REFUSED: kg-sync helper not found — dropping" >&2
    echo "[migrate-shared-kg] $SHARED_KG now would leave it empty with no" >&2
    echo "[migrate-shared-kg] repopulation path. Run 'python install.py --update'" >&2
    echo "[migrate-shared-kg] (which materializes .claude/scripts/) and retry." >&2
    exit 4
fi

# ---------------------------------------------------------------------------
# GUARD 2 (BEFORE drop): cross-project shared-write probe. The resync only
# restores nodes whose .md source lives under THIS clone. Enumerate stored
# file_path values and flag every node we cannot restore. Conservative by
# design: a failed probe, an over-window collection (>10000 objects), or a
# single unrecoverable path all REFUSE unless consent is given.
# ---------------------------------------------------------------------------
PROBE_LIMIT=10000

TOTAL_COUNT="$(curl -sS -X POST -H 'Content-Type: application/json' \
    -d "{\"query\":\"{ Aggregate { $SHARED_KG { meta { count } } } }\"}" \
    "$WEAVIATE_URL/v1/graphql" 2>/dev/null \
    | jq -r ".data.Aggregate.${SHARED_KG}[0].meta.count // \"probe-failed\"" \
    2>/dev/null || echo "probe-failed")"

UNRECOVERABLE=0
UNRECOVERABLE_SAMPLES=""
PROBE_OK=1

if ! [ "$TOTAL_COUNT" -ge 0 ] 2>/dev/null; then
    # Non-numeric (probe-failed / null / empty) → cannot verify.
    PROBE_OK=0
elif [ "$TOTAL_COUNT" -gt "$PROBE_LIMIT" ]; then
    # Beyond the probe window — cannot verify every node. Treat as
    # unverifiable rather than silently checking only the first page.
    PROBE_OK=0
elif [ "$TOTAL_COUNT" -gt 0 ]; then
    FILE_PATHS="$(curl -sS -X POST -H 'Content-Type: application/json' \
        -d "{\"query\":\"{ Get { $SHARED_KG(limit: $PROBE_LIMIT) { file_path } } }\"}" \
        "$WEAVIATE_URL/v1/graphql" 2>/dev/null \
        | jq -r ".data.Get.${SHARED_KG}[]?.file_path // empty" 2>/dev/null \
        | sort -u)" || PROBE_OK=0

    if [ "$PROBE_OK" = "1" ]; then
        while IFS= read -r FP; do
            [ -z "$FP" ] && continue
            case "$FP" in
                /*)
                    # Absolute path: restorable only if it points inside
                    # this clone AND still exists (kg-sync walks the
                    # clone's knowledge/ tree).
                    case "$FP" in
                        "$CLONE_ROOT"/*) [ -f "$FP" ] && continue ;;
                    esac
                    ;;
                *)
                    # Relative path: restorable iff it resolves under the
                    # clone root.
                    [ -f "$CLONE_ROOT/$FP" ] && continue
                    ;;
            esac
            UNRECOVERABLE=$((UNRECOVERABLE + 1))
            if [ "$UNRECOVERABLE" -le 10 ]; then
                UNRECOVERABLE_SAMPLES="${UNRECOVERABLE_SAMPLES}
[migrate-shared-kg]     - $FP"
            fi
        done <<EOF
$FILE_PATHS
EOF
    fi
fi

if [ "$PROBE_OK" != "1" ] || [ "$UNRECOVERABLE" -gt 0 ]; then
    if [ "$CONSENT" = "1" ]; then
        if [ "$PROBE_OK" != "1" ]; then
            echo "[migrate-shared-kg] WARNING: safety probe could not verify the" >&2
            echo "[migrate-shared-kg] collection (count=$TOTAL_COUNT) but" >&2
            echo "[migrate-shared-kg] VCO_SHARED_KG_MIGRATE_CONSENT=1 — proceeding." >&2
        else
            echo "[migrate-shared-kg] WARNING: $UNRECOVERABLE cross-project shared" >&2
            echo "[migrate-shared-kg] node(s) will be PERMANENTLY LOST (consented)." >&2
        fi
    else
        if [ "$PROBE_OK" != "1" ]; then
            echo "[migrate-shared-kg] REFUSED: could not verify that every shared" >&2
            echo "[migrate-shared-kg] node is restorable from this clone" >&2
            echo "[migrate-shared-kg] (object count: $TOTAL_COUNT; probe window: $PROBE_LIMIT)." >&2
        else
            echo "[migrate-shared-kg] REFUSED: $UNRECOVERABLE shared node(s) were" >&2
            echo "[migrate-shared-kg] written by OTHER projects (or have sources" >&2
            echo "[migrate-shared-kg] outside this clone) and would be" >&2
            echo "[migrate-shared-kg] PERMANENTLY LOST by drop+resync. Samples:$UNRECOVERABLE_SAMPLES" >&2
        fi
        echo "[migrate-shared-kg] To proceed anyway (accepting the loss):" >&2
        echo "[migrate-shared-kg]   VCO_SHARED_KG_MIGRATE_CONSENT=1 WEAVIATE_URL='$WEAVIATE_URL' \\" >&2
        echo "[migrate-shared-kg]     bash scripts/migrate-shared-kg-schema.sh" >&2
        echo "[migrate-shared-kg] Better: re-run each contributing project's" >&2
        echo "[migrate-shared-kg] '.claude/scripts/kg-sync --all' AFTER the migration" >&2
        echo "[migrate-shared-kg] to restore its shared nodes." >&2
        exit 3
    fi
fi

echo "[migrate-shared-kg] Dropping + recreating $SHARED_KG ..."

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
