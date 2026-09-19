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

v0.2.95 (WP-9) adds :func:`reconcile_schema_migration_deferral`, the GATE that
drives the emitter — "may this dry-run write the entry, and does a clean one
clear a stale entry?". It sat inline in ``_cmd_migrate_collections``, one
caller and ~95 lines away from the emitter whose whole contract it encodes.
Same rationale as the emitter's own move: one concern, one home, and the
ratchet-capped module shrinks rather than grows.
"""

from __future__ import annotations

import sys
from pathlib import Path

from vco_lib import migration_plan_classify as _mpc

__all__ = [
    "_emit_migrate_required_deferral",
    "reconcile_schema_migration_deferral",
]


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


def reconcile_schema_migration_deferral(
    result: dict,
    *,
    project_folder,
    project_name: str,
    weaviate_url: str,
    dry_run: bool,
    all_projects: bool,
) -> None:
    """Emit — or clear — `schema_migration_required` for a dry-run plan.

    Moved verbatim out of `project_init._cmd_migrate_collections` in v0.2.95
    (WP-9); `result` is MUTATED in place, as it was there: `deferral_emitted` /
    `stale_migrate_deferral_cleared` are set, and both failure paths append to
    `errors[]` rather than raising.

    The GATE (all four conditions) is part of the policy, so it moved with it:
    a deferral is only written for a project-scoped, error-free DRY RUN. A wet
    run has already done the work; an `--all-projects` sweep has no single
    folder to write into; a probe that errored saw the drift only partly.

    v0.2.70: `copy` is ALWAYS lossless — the staging double-copy round-trips
    every EXISTING UUID + named vector + property byte-for-byte via
    `_copy_collection_with_vectors` (no re-embedding; the live collection is
    not dropped until the staging swap's count-match assertion passes). So
    `copy` must AUTO-APPLY without consent — only genuinely data-losing actions
    defer, and `rebuild` is the exact lossy set. `legacy_single_vector`
    classifies `rebuild` (never `copy`). A same-name/different-dim slot is
    INVISIBLE to `_schema_delta` (name-only comparison): on its own it yields
    `noop`; when it COEXISTS with a genuinely-missing slot, `_classify_action`
    returns `copy` (driven by the missing slot) and the mismatch slot rides
    along — but copy still only round-trips the EXISTING vectors verbatim (it
    neither fixes nor worsens the dim-mismatch, and never re-embeds/drops), so
    it remains lossless + data-safe. Genuine dim-mismatch remediation is owned
    by the schema_migration_runner subsystem (it defers). The dry-run plan
    strips `delta`, leaving `action` as the only signal here — sufficient given
    that proof.

    NOTE: this auto-apply is NEW behavior, NOT a mirror of `install.py
    --update` (whose drift detector EXCLUDES the additive v0.2.18 slots and
    never reaches the apply for an additive 3->5 drift). It is justified purely
    by losslessness. The launcher's WET follow-up that actually applies the
    additive subset lives in `projects_v2.rs::run_migrate_dry_run` (the dry-run
    probe only stops deferring — it never mutates).

    v0.2.95 WP-9: "which actions are lossy" is no longer decided here either.
    `vco_lib.migration_plan_classify` owns it and also computes the
    `auto_apply_additive` verdict the envelope publishes, so "what defers" and
    "what the launcher may apply unattended" cannot drift apart — they used to
    be a Python list comprehension and a Rust predicate.
    """
    if not (project_folder and dry_run and not result["errors"]
            and not all_projects):
        return

    destructive = _mpc.lossy_plan_entries(result)
    resolved_folder = Path(project_folder).resolve()
    if destructive:
        try:
            _emit_migrate_required_deferral(
                resolved_folder,
                project_name=project_name,
                weaviate_url=weaviate_url,
                plan_entries=destructive,
            )
            result["deferral_emitted"] = True
        except Exception as e:
            # Soft-fail: a deferral write failure must not abort the whole
            # update flow. Report via errors[] so the Rust caller surfaces it
            # as a warning toast.
            result["errors"].append({
                "collection": None,
                "action": "deferral",
                "error": f"migrate-required deferral write failed: "
                         f"{type(e).__name__}: {e}",
            })
    else:
        # v0.2.55 (stale-migration-deferral fix): the dry-run is CLEAN (no
        # copy/rebuild needed). PRE-v0.2.55 this branch did nothing, so a
        # `schema_migration_required` entry written by an EARLIER update (when
        # a migration WAS pending) survived forever even after the migration
        # was applied or the schema healed — exactly the stale-deferral
        # carry-forward bug (the entry was re-read by `DeferralReport.read()`
        # on every subsequent bundle update and never cleared because the
        # emitter is gated on `destructive`). Re-probe-clears-stale, matching
        # the Track D `--apply-deferred` discipline: a clean dry-run IS the
        # re-probe; clear the stale entry. Soft-fail — never abort the update
        # over a deferral housekeeping write.
        try:
            # v0.2.83 PLAN-v0283 WP-B2: resolve via the ONE locked emitter
            # home (read-modify-write under the exclusive lock; foreign
            # entries preserved). resolve_conditions returns the count it
            # actually cleared, so the flag is set only when it fired.
            from vco_lib import deferral_emit as _de
            cleared = _de.resolve_conditions(
                resolved_folder, ["schema_migration_required"],
            )
            if cleared:
                result["stale_migrate_deferral_cleared"] = True
                # stderr (not stdout) so `--json` output stays parseable.
                print(
                    "  [ok] schema_migration_required: dry-run clean — "
                    "cleared stale migration deferral (no copy/rebuild "
                    "needed).",
                    file=sys.stderr,
                )
        except Exception as e:
            # Housekeeping only — report but don't fail.
            result["errors"].append({
                "collection": None,
                "action": "deferral-clear",
                "error": f"stale migrate-deferral clear failed: "
                         f"{type(e).__name__}: {e}",
            })
