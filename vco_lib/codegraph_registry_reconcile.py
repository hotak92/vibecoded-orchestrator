# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One-time code-graph registry reconcile (A3, v0.2.74 migration delivery).

The problem A3 closes
---------------------
Existing users (installed at ANY prior version) may have a code-graph
collection with a STALE or MISSING ``artifact_schema_versions`` registry row —
because the version-gated runner never ran the codegraph migration for them
(A1: install.py called ``run_schema_migrations`` with an env that had no
``CODE_GRAPH_PROJECT``, so the codegraph loop iterated zero times). A1 fixes
the FORWARD path; A3 is the one-time BACKFILL that carries the already-installed
population to a correct registry state.

What it does
------------
On the 0.2.74 update, for EACH project that owns a
``project_codegraph_bindings`` row (the SSOT for the ``<prefix>_Code*`` class
names) whose codegraph collection EXISTS WITH DATA in Weaviate but whose
registry row is stale/missing:

  * replay the WHOLE contiguous edge ladder from the EARLIEST edge via
    ``schema_migration_runner._apply_edges_preserving`` (idempotent-from-
    earliest: 4_to_5 / 5_to_6 are add-property-if-absent, 6_to_7 is
    exact-Python-substring delete-only — replaying an already-applied edge is a
    no-op), and
  * register at canonical (``_apply_edges_preserving`` does this on the final
    edge).

Because the ladder is idempotent-from-earliest, we do NOT need to "guess the
true version" — we just replay from earliest. The augmented env carries
``CODE_GRAPH_PROJECT`` (the resolved prefix) down to each edge subprocess so the
edge's OWN ``_resolve_codegraph_prefix()`` resolves the same prefix (and the
HIGH-2 sentinel check confirms the edge actually applied rather than no-op'd).

Design contract (mirrors ``vco_lib.kg_binding_heal``)
-----------------------------------------------------
* launcher.db is READ-only via the RO-URI helper in ``launcher_db_reader`` —
  the vct-hub holds the single-writer WAL lock, so a plain connect would
  return "empty" / lock.
* Per-binding loop, soft-fail: a hiccup on one project WARNs + defers, never
  crashes ``install.py --update`` and never aborts the OTHER projects.
* Registry writes go through ``artifact_version_registry.register_artifact_
  version`` (the single registry writer), invoked by ``_apply_edges_preserving``.
* NEVER drops/recreates a collection; never re-embeds. Only add-property /
  delete-specific-rows edges run.

Wiring
------
``install.py``'s ``--update`` path calls :func:`reconcile_codegraph_registry`
ONCE, soft-fail, AFTER the main ``run_schema_migrations`` pass (which handles
the ROOT project; the reconcile sweeps EVERY registered project so non-root
existing projects are carried forward too).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = ["reconcile_codegraph_registry", "ReconcileOutcome"]


class ReconcileOutcome:
    """Mutable tally of a reconcile pass (returned for logging / tests)."""

    def __init__(self) -> None:
        #: (project_id, prefix) tuples whose ladder was replayed + registered.
        self.reconciled: list[tuple[str, str]] = []
        #: (project_id, prefix) tuples SKIPPED because the collection has no
        #: data / could not be confirmed to exist (nothing to reconcile).
        self.skipped_no_data: list[tuple[str, str]] = []
        #: (project_id, prefix) tuples already UP_TO_DATE in the registry.
        self.already_current: list[tuple[str, str]] = []
        #: (project_id, prefix, detail) tuples that DEFERRED (edge error /
        #: no-prefix sentinel / DB error) — retried next update.
        self.deferred: list[tuple[str, str, str]] = []

    def to_dict(self) -> dict:
        return {
            "reconciled": [
                {"project_id": pid, "prefix": pre}
                for (pid, pre) in self.reconciled
            ],
            "skipped_no_data": [
                {"project_id": pid, "prefix": pre}
                for (pid, pre) in self.skipped_no_data
            ],
            "already_current": [
                {"project_id": pid, "prefix": pre}
                for (pid, pre) in self.already_current
            ],
            "deferred": [
                {"project_id": pid, "prefix": pre, "detail": detail}
                for (pid, pre, detail) in self.deferred
            ],
        }


def _read_codegraph_bindings_ro(db_path: Path) -> list[tuple[str, str]]:
    """Return ``[(project_id, collection_prefix), ...]`` for every project that
    owns a ``project_codegraph_bindings`` row, read via RO-URI (never blocks on
    the hub's WAL write lock). Returns ``[]`` on any error / missing table.

    Reuses the ``launcher_db_reader`` RO-connect discipline: the bindings are
    the SSOT for the ``<prefix>_Code*`` class names. Only rows with an
    ``enabled != 0`` flag AND a non-empty prefix are returned — a disabled
    codegraph binding means the user turned code-graph off for that project, so
    we must not reconcile its (possibly stale) classes.
    """
    from . import launcher_db_reader as ldr

    conn = ldr._open_db_readonly(db_path)  # RO-URI connect (mode=ro) on THIS db
    if conn is None:
        return []
    try:
        try:
            rows = conn.execute(
                "SELECT project_id, collection_prefix, enabled "
                "FROM project_codegraph_bindings"
            ).fetchall()
        except Exception:
            return []
        out: list[tuple[str, str]] = []
        for r in rows:
            # enabled column may be absent on a very old schema — default to
            # enabled (row_factory is sqlite3.Row so key access is safe).
            try:
                enabled = r["enabled"]
            except (IndexError, KeyError):
                enabled = 1
            if enabled is not None and int(enabled) == 0:
                continue
            pid = (r["project_id"] or "").strip()
            prefix = (r["collection_prefix"] or "").strip()
            if pid and prefix:
                out.append((pid, prefix))
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def reconcile_codegraph_registry(
    deferral_report: object = None,
    *,
    db_path: Path,
    weaviate_url: str = "http://localhost:8081",
    migrations_dir: Path,
    project_root: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    log_event: Optional[Callable[..., None]] = None,
    deferral_entry_cls: object = None,
    now_ms: Optional[int] = None,
) -> ReconcileOutcome:
    """One-time backfill: carry EVERY registered project's code-graph
    collection to a correct registry state via the idempotent-from-earliest
    edge ladder. Soft-fail throughout; never raises into the caller.

    Args:
        deferral_report: a ``DeferralReport``-like with ``add_entry`` — receives
            one ``codegraph_registry_reconcile_deferred`` entry when >=1 project
            deferred (edge error / no-prefix / DB error). ``None`` skips the
            deferral write (tests).
        db_path: launcher.db (the registry + bindings SSOT live here).
        weaviate_url: target Weaviate for the existence probe + edge scripts.
        migrations_dir: ``<root>/migrations`` — the edge ladder lives under
            ``migrations/codegraph_collection/``.
        project_root: cwd for edge subprocesses (defaults to migrations_dir's
            parent, the orchestrator clone root).
        env: base env threaded to edge subprocesses. ``CODE_GRAPH_PROJECT`` is
            OVERRIDDEN per-project with the SSOT prefix (so each project's edges
            resolve their own scope regardless of the process env).
        log_event: ``(stage, level, msg, *, data=None)`` sink (optional).
        deferral_entry_cls: the caller's ``DeferralEntry`` dataclass (injected
            to avoid importing install.py). When ``None`` the module imports
            ``vco_lib.deferral_report.DeferralEntry`` itself.
        now_ms: override materialized_at (testing).

    Returns:
        :class:`ReconcileOutcome` tallying reconciled / skipped / deferred.
    """
    from . import artifact_version_registry as avr
    from . import schema_migration_runner as smr
    from . import schema_versions as sv

    outcome = ReconcileOutcome()

    def _log(stage: str, level: str, msg: str, **kw) -> None:
        if log_event is not None:
            try:
                log_event(stage, level, msg, **kw)
            except Exception:
                pass

    root = project_root or migrations_dir.parent
    base_env = dict(env) if env is not None else {}

    try:
        bindings = _read_codegraph_bindings_ro(db_path)
    except Exception as exc:  # never crash the update on a DB read
        _log("7d/10", "warn", f"codegraph reconcile: bindings read failed: {exc}")
        return outcome

    if not bindings:
        _log(
            "7d/10", "ok",
            "codegraph reconcile: no code-graph bindings to reconcile",
        )
        return outcome

    try:
        canonical = sv.canonical_version("codegraph_collection")
    except Exception as exc:  # unknown artifact_type → nothing we can do
        _log("7d/10", "warn", f"codegraph reconcile: canonical read failed: {exc}")
        return outcome

    edges = smr.discover_edges(migrations_dir, "codegraph_collection")
    if not edges:
        # No ladder shipped → nothing to replay (the collections are, by
        # definition of an empty ladder, at canonical already).
        _log(
            "7d/10", "ok",
            "codegraph reconcile: no edge ladder shipped; nothing to replay",
        )
        return outcome
    derived = True  # codegraph_collection is derived by construction.

    for project_id, prefix in bindings:
        class_names = smr.codegraph_class_names_for_prefix(prefix)
        if not class_names:
            continue

        # 1. Is the registry already current for this project's codegraph? The
        #    5 class names share one recorded version — probe the first.
        try:
            status = avr.check_artifact_version(
                db_path,
                project_id=project_id,
                artifact_type="codegraph_collection",
                artifact_name=class_names[0],
            )
        except Exception as exc:
            outcome.deferred.append(
                (project_id, prefix, f"registry check failed: {exc}")
            )
            continue
        if status == avr.ArtifactVersionStatus.UP_TO_DATE:
            outcome.already_current.append((project_id, prefix))
            continue
        if status == avr.ArtifactVersionStatus.REFUSE_DOWNGRADE:
            # launcher.db written by a newer orchestrator — never mangle.
            outcome.deferred.append(
                (project_id, prefix, "recorded version newer than canonical")
            )
            continue

        # 2. Does the collection actually EXIST WITH DATA? A born-fresh /
        #    absent collection needs no reconcile (the forward NEVER_MATERIALIZED
        #    path stamps it at canonical). None (unknown / Weaviate down) →
        #    conservative skip: retried next update when Weaviate is reachable.
        has_rows = smr._codegraph_collection_has_rows(weaviate_url, class_names)
        if not has_rows:
            outcome.skipped_no_data.append((project_id, prefix))
            continue

        # 3. Replay the WHOLE ladder from earliest (idempotent-from-earliest).
        #    Per-project env override so the edge subprocesses resolve THIS
        #    project's scope regardless of the install.py process env.
        proj_env = dict(base_env)
        proj_env["CODE_GRAPH_PROJECT"] = prefix

        report = smr.MigrationRunReport()
        try:
            smr._apply_edges_preserving(
                artifact_type="codegraph_collection",
                artifact_name=class_names[0],
                edges=edges,
                stored=edges[0].from_version,
                canonical=canonical,
                derived=derived,
                env=proj_env,
                weaviate_url=weaviate_url,
                launcher_db=db_path,
                project_id=project_id,
                root=root,
                report=report,
                check=False,
                when=now_ms if now_ms is not None else _now_ms(),
                register_on_success=True,
            )
        except Exception as exc:  # never let one project abort the sweep
            outcome.deferred.append(
                (project_id, prefix, f"ladder replay raised: {exc}")
            )
            continue

        if report.errors:
            # HIGH-2 no-prefix sentinel or a genuine edge failure — do NOT count
            # as reconciled; the recorded version was left at `stored`.
            detail = "; ".join(d for (_t, _n, d) in report.errors)[:300]
            outcome.deferred.append((project_id, prefix, detail))
        elif report.applied:
            # _apply_edges_preserving registered ONLY class_names[0] at canonical
            # (it takes ONE artifact_name). The codegraph_collection has 5
            # registry rows (one per <prefix>_Code* class) that all share the
            # recorded version — register the OTHER 4 at canonical too so the
            # whole collection reads UP_TO_DATE next check (the forward runner's
            # per-class loop does this naturally; the reconcile runs the ladder
            # ONCE so it must register the siblings explicitly).
            for extra_name in class_names[1:]:
                avr.register_artifact_version(
                    db_path,
                    project_id=project_id,
                    artifact_type="codegraph_collection",
                    artifact_name=extra_name,
                    schema_version=canonical,
                    materialized_at=now_ms if now_ms is not None else _now_ms(),
                )
            outcome.reconciled.append((project_id, prefix))
        else:
            # No edges applied and no errors — treat as skip (nothing to do).
            outcome.skipped_no_data.append((project_id, prefix))

    _log(
        "7d/10", "ok",
        (
            f"codegraph reconcile: {len(outcome.reconciled)} reconciled, "
            f"{len(outcome.already_current)} already-current, "
            f"{len(outcome.skipped_no_data)} skipped(no-data), "
            f"{len(outcome.deferred)} deferred"
        ),
        data=outcome.to_dict(),
    )

    # Emit ONE deferral summarising the deferred projects (retried next update).
    if outcome.deferred and deferral_report is not None:
        _emit_deferral(deferral_report, outcome, deferral_entry_cls)

    return outcome


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _emit_deferral(
    deferral_report: object,
    outcome: ReconcileOutcome,
    deferral_entry_cls: object,
) -> None:
    """Add a single ``codegraph_registry_reconcile_deferred`` entry listing the
    projects whose ladder replay could not complete. Soft-fail: a deferral-write
    error never crashes the reconcile."""
    try:
        if deferral_entry_cls is None:
            from .deferral_report import DeferralEntry as _DE

            deferral_entry_cls = _DE
        lines = "\n".join(
            f"  * project_id={pid} prefix=`{pre}`: {detail}"
            for (pid, pre, detail) in outcome.deferred
        )
        deferral_report.add_entry(  # type: ignore[attr-defined]
            deferral_entry_cls(  # type: ignore[operator]
                condition_id="codegraph_registry_reconcile_deferred",
                title=(
                    f"Code-graph registry reconcile incomplete for "
                    f"{len(outcome.deferred)} project(s)"
                ),
                detected=(
                    "The one-time v0.2.74 code-graph registry reconcile (A3) "
                    "could not complete the schema-ladder replay for the "
                    "following project(s). Nothing was dropped; the recorded "
                    "schema version was NOT advanced for these — they retry on "
                    "the next `install.py --update`.\n\n" + lines
                ),
                why_deferred=(
                    "A reconcile replay is deferred when an edge subprocess "
                    "reported it could not resolve the code-graph scope "
                    "(EDGE_NOOP_NO_PREFIX), when Weaviate was unreachable, or "
                    "when a per-project registry read failed. All three are "
                    "transient — re-running the update once Weaviate is up (and "
                    "the code-graph binding resolves) completes the reconcile."
                ),
                command_to_apply=(
                    "# Ensure Weaviate is running, then re-run the update:\n"
                    "python install.py --update"
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )
    except Exception as exc:  # never block on a deferral write
        logger.debug(
            "codegraph reconcile: deferral write failed (non-fatal): %s", exc
        )
