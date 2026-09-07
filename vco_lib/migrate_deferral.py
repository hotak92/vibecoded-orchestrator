# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``schema_migration_required`` deferral emitter.

Extracted from ``vco_lib/project_init.py`` (v0.2.92). That module is
ratchet-capped and must SHRINK, not grow; this emitter is ~120 lines of
self-contained string building with a single production caller and no
dependency on project_init's state, so it is the natural thing to lift out
when the cap needs headroom.

``project_init`` re-exports the name, because the existing tests reach it as
``project_init._emit_migrate_required_deferral`` and a move should not be
allowed to look like a behaviour change.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["_emit_migrate_required_deferral"]


def _emit_migrate_required_deferral(
    folder: Path,
    *,
    project_name: str,
    weaviate_url: str,
    plan_entries: list[dict],
) -> None:
    """Emit `schema_migration_required`: a Weaviate dry-run plan revealed
    one or more collections need a LOSSY `rebuild` (drop + re-embed via
    Ollama) to reach the target schema. `rebuild` regenerates vectors
    rather than preserving them, so we DO NOT auto-apply it — we surface a
    deferral entry that names each collection + its required action and tells
    the user the explicit command to consent.

    v0.2.70: additive `copy` migrations are LOSSLESS (staging double-copy
    that round-trips every UUID + named vector + property byte-for-byte; copy
    never re-embeds and never drops the live collection) and are AUTO-APPLIED
    without a deferral. The caller (`_cmd_migrate_collections` gate) filters
    the plan to `action == "rebuild"` before calling this emitter, so it is
    only ever invoked with lossy rebuild entries.

    Args:
        folder: target user-project folder.
        project_name: raw project name (the user-facing label).
        weaviate_url: the URL the dry-run probed (echoed in the command_to_apply).
        plan_entries: list of `{"collection", "action"}` dicts where action is
            `rebuild` (legacy single-vector or unhandled escape). The gate
            filters out additive `copy` before this call.

    Severity is `warning`: the project is functional with the existing schema
    (read paths still work), but new schema features (e.g. `index_null_state`)
    are missing until the user explicitly consents to migrate.
    """
    if not plan_entries:
        return
    from vco_lib.deferral_report import DeferralEntry
    from vco_lib import deferral_emit as _de

    # Render the per-collection action plan as a bullet list. Sorted for
    # determinism so deferral .md doesn't churn between runs that produce
    # the same plan in different order.
    detected_lines = []
    for entry in sorted(plan_entries, key=lambda e: (e.get("collection") or "", e.get("action") or "")):
        coll = entry.get("collection") or "?"
        # v0.2.70: the gate (`_cmd_migrate_collections`) filters the plan to
        # `action == "rebuild"` before calling this emitter, so every entry
        # here is a lossy rebuild (legacy single-vector or unhandled escape).
        # Additive `copy` is auto-applied, never deferred.
        detected_lines.append(
            f"  - `{coll}` → **rebuild** (drop + re-embed; legacy single-vector format)"
        )

    # Build the suggested command. `vco_lib` lives in the ORCHESTRATOR
    # clone's venv (NOT this project's venv) — running `python -m
    # vco_lib.project_init ...` from the project directory fails with
    # ModuleNotFoundError. The command below uses an explicit
    # `cd $VCT_ORCHESTRATOR_ROOT && .venv/bin/python -m ...` invocation
    # so the user (or an LLM agent reading this) doesn't have to figure
    # out the venv plumbing. `--name '<project>'` scopes the migration
    # to THIS project's collections regardless of where the orchestrator
    # clone lives. (v0.2.18 doc fix 2026-05-19: prior wording assumed
    # the user knew to run from VCT_ORCHESTRATOR_ROOT.)
    #
    # v0.2.54 Track D (P0-2): both commands now pass `--project-folder`
    # so the CLI's post-rebuild re-ingest step can locate the project's
    # `.claude/scripts/sync_knowledge_graph.py` and restore the dropped
    # data immediately. Pre-fix the command promised "falls back to
    # drop+re-embed" while the CLI path never re-embedded — the user's
    # collection stayed empty until the next full install.py run.
    #
    # v0.2.70: this emitter only fires for lossy `rebuild` now (additive
    # `copy` is auto-applied), so the command always documents the
    # drop + recreate + re-ingest path. The smart `migrate-collections`
    # call preserves vectors via copy where possible and only rebuilds the
    # legacy collections; `--force-rebuild` is the all-collections escape.
    folder_arg = f"--project-folder {str(folder)!r} "
    cmd = (
        f"# Run the migration from the orchestrator clone (vco_lib lives there,\n"
        f"# NOT in this project's venv). The --name flag scopes the work to\n"
        f"# THIS project's collections. Preserves vectors via copy where possible;\n"
        f"# for legacy single-vector collections it drops, recreates with the\n"
        f"# target schema, and re-ingests from knowledge/ + docs/ (requires the\n"
        f"# embedding backend to be healthy; ~3-5 min).\n"
        f"#   POSIX:   cd \"$VCT_ORCHESTRATOR_ROOT\" && .venv/bin/python -m ...\n"
        f"#   Windows: cd $env:VCT_ORCHESTRATOR_ROOT; .venv\\Scripts\\python.exe -m ...\n"
        f"cd \"$VCT_ORCHESTRATOR_ROOT\" && .venv/bin/python -m vco_lib.project_init migrate-collections "
        f"--name {project_name!r} --weaviate-url {weaviate_url!r} "
        f"{folder_arg}--json\n"
        f"# OR force the destructive drop+recreate+re-ingest for ALL collections\n"
        f"# (slower; same embedding-backend requirement):\n"
        f"cd \"$VCT_ORCHESTRATOR_ROOT\" && .venv/bin/python -m vco_lib.project_init migrate-collections "
        f"--name {project_name!r} --weaviate-url {weaviate_url!r} "
        f"{folder_arg}--force-rebuild --json"
    )

    entry = DeferralEntry(
        condition_id="schema_migration_required",
        title="Schema migration required",
        detected=(
            f"A pre-update dry-run of `migrate-collections` against "
            f"`{weaviate_url}` reported one or more per-project Weaviate "
            f"collections need a data-rebuilding migration (drop + re-embed) "
            f"to reach the current target schema:\n"
            + "\n".join(detected_lines)
        ),
        # must match projects_v2.rs run_migrate_dry_run warning (cross-language
        # mirror — see launcher/src-tauri/src/commands/projects_v2.rs). Keep the
        # framing semantically identical: rebuild re-embeds (vectors regenerated,
        # not preserved) so it is consent-gated; additive copy is lossless and
        # auto-applied without a deferral.
        why_deferred=(
            "Schema drift detected. `rebuild` re-embeds every object via Ollama "
            "(vectors are regenerated, not preserved), so it is deferred for "
            "explicit consent. Additive `copy` migrations preserve all data "
            "(UUIDs + named vectors + properties round-trip byte-for-byte) and "
            "are auto-applied without a deferral. The bundle install (hooks, "
            "agents, scripts) still proceeds and is unaffected."
        ),
        command_to_apply=cmd,
        severity="warning",
        kg_node_refs=[],
    )
    # v0.2.83 PLAN-v0283 WP-B2: emit via the ONE locked emitter home.
    _de.emit(folder, entry)
