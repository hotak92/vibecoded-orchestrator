# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""POLICY STEP 3 "Regenerate now" — the guarded drop+recreate+re-sync action.

This is the Piece-4 realization of the launcher's **"Regenerate now"** modal
choice (PLAN-v0260-consolidated-update-system.md §"Piece 4 scope decision",
SPEC-v0260-migration-runner.md §2.9/§6.1). When a DERIVED Weaviate collection
is stale + schema-changed + has NO data-preserving migration edge, the runner
records a ``pending_regenerate`` entry; the user must then explicitly choose to
**regenerate** (drop + recreate + re-sync from disk) or **defer** (write
``UPDATE_DEFERRED.md``). The drop is NEVER automatic — it requires the explicit
click that lands here.

This module does NOT invent a new drop path. It composes the EXISTING,
already-audited machinery:

  * **shared KG** (`shared_kg_collection`) → the existing guarded
    ``scripts/migrate-shared-kg-schema.{sh,ps1}`` body, which enforces
    GUARD 1 (kg-sync helper present → exit 4) and GUARD 2 (cross-project
    shared-write probe → exit 3 unless ``VCO_SHARED_KG_MIGRATE_CONSENT=1``).
    Reusing the script means the data-safety guards still gate the drop.

  * **per-project KG / Development / Diagrams** (`kg_collection`,
    `development_collection`, `diagrams_collection`) → the existing
    ``migrate-collections --name <project> --force-rebuild --project-folder
    <folder>`` CLI, which drops + recreates with the canonical schema and
    re-runs ``.claude/scripts/sync_knowledge_graph.py --all`` to re-ingest
    from ``knowledge/**`` / ``docs/**``. NO new drop path.

  * **codegraph** (`codegraph_collection`) → re-run the project's
    ``.claude/scripts/code-graph-analyze . --force-recreate`` which drops +
    rebuilds the 5 ``<prefix>_Code*`` classes from the source walk. NO new
    drop path. (v0.2.75: docs previously said ``--force``, a flag the
    analyzer's argparse rejects — the RUNNER below always used the real
    ``--force-recreate``.)

After a successful regenerate, the artifact is re-registered at canonical via
``vco_lib.artifact_version_registry.register_artifact_version`` (preceded by an
idempotent ``unregister`` so the row is rewritten cleanly). On a GUARD refusal
(exit 3/4) NOTHING is dropped and the refusal reason is surfaced verbatim — the
launcher modal shows it + the ``VCO_SHARED_KG_MIGRATE_CONSENT=1`` escalation.

The function is invoked from ``vco_lib.project_init._cmd_migrate_schema`` when
``--regenerate <artifact_type> --artifact-name <name>`` is passed (the launcher's
``apply_stale_derived_choice(choice="regenerate")`` Tauri command), and is unit-
tested directly (the subprocess/script calls are injectable for tests via the
``runner`` parameter so no live Weaviate / shell is needed).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import artifact_version_registry as avr
from . import schema_versions as sv

logger = logging.getLogger(__name__)

__all__ = [
    "RegenerateResult",
    "regenerate_derived_collection",
    "build_reingest_incomplete_entry",
    "slugify_artifact_name",
]

#: Generous timeout for a full drop+recreate+re-sync (re-embed can be slow on a
#: large KG / codegraph). Mirrors the 900s the migrate-collections re-ingest
#: uses (project_init.py:8517).
_REGEN_TIMEOUT = 900

#: The shared-KG artifact_type whose regenerate routes through the EXISTING
#: guarded migrate-shared-kg-schema script (GUARD 1/2 enforced there).
_SHARED_KG_TYPE = "shared_kg_collection"

#: Per-project single-class Weaviate collections whose regenerate routes
#: through ``migrate-collections --force-rebuild``.
_PER_PROJECT_COLLECTION_TYPES = frozenset(
    {"kg_collection", "development_collection", "diagrams_collection"}
)

#: Codegraph artifact_type whose regenerate re-runs code-graph-analyze.
_CODEGRAPH_TYPE = "codegraph_collection"


@dataclass
class RegenerateResult:
    """Outcome of a single-artifact "Regenerate now" action.

    ``ok`` is True only when the destructive recreate ACTUALLY completed AND
    the artifact was re-registered at canonical. ``refused`` is True when an
    existing data-safety guard (GUARD 1/2 in the shared-KG script) blocked the
    drop — in that case NOTHING was dropped and ``detail`` carries the refusal
    reason + escalation command. ``error`` is for any other failure (subprocess
    spawn error, re-ingest non-zero, registry write failure).
    """

    artifact_type: str
    artifact_name: str
    ok: bool = False
    refused: bool = False
    dropped: bool = False
    registered: bool = False
    #: C1: True when the drop completed but the re-ingest/re-analyze did NOT
    #: confirm (e.g. migrate-collections returned reingest_required=true). The
    #: collection is empty-on-Weaviate but its source is on disk → recoverable.
    #: The caller MUST write a ``schema_reingest_incomplete_<slug>`` deferral so
    #: a later update/session re-ingests rather than the runner silently
    #: registering the empty collection at canonical (NEVER_MATERIALIZED path).
    reingest_incomplete: bool = False
    detail: str = ""
    error: Optional[str] = None
    #: stderr tail from the underlying command (for the modal's detail view).
    stderr_tail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "artifact_type": self.artifact_type,
            "artifact_name": self.artifact_name,
            "ok": self.ok,
            "refused": self.refused,
            "dropped": self.dropped,
            "registered": self.registered,
            "reingest_incomplete": self.reingest_incomplete,
            "detail": self.detail,
            "error": self.error,
            "stderr_tail": self.stderr_tail,
        }


def _stderr_tail(stderr: str, n: int = 6) -> list[str]:
    lines = [ln for ln in stderr.splitlines() if ln.strip()]
    return lines[-n:]


def regenerate_derived_collection(
    *,
    artifact_type: str,
    artifact_name: str,
    folder: Path,
    db_path: Path,
    project_id: Optional[str],
    project_name: Optional[str],
    env: Mapping[str, str],
    weaviate_url: str,
    when: int,
    orchestrator_root: Path,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> RegenerateResult:
    """Perform the guarded "Regenerate now" recreate for ONE derived collection.

    Dispatch (NO new drop path — every branch composes existing machinery):

      * ``shared_kg_collection`` → ``scripts/migrate-shared-kg-schema.{sh,ps1}``
        (GUARD 1/2 still gate the drop; exit 3 = refused cross-project, exit 4 =
        kg-sync helper missing).
      * ``kg_collection`` / ``development_collection`` / ``diagrams_collection``
        → ``python -m vco_lib.project_init migrate-collections --name <project>
        --force-rebuild --project-folder <folder>`` (drop+recreate+re-sync from
        ``knowledge/**`` / ``docs/**``).
      * ``codegraph_collection`` → ``.claude/scripts/code-graph-analyze .
        --force-recreate`` (drop+rebuild the 5 Code* classes from the walk).

    On success, re-registers the artifact at canonical (idempotent
    unregister→register). On a GUARD refusal, NOTHING is dropped and the result
    carries ``refused=True`` + the refusal reason.

    Args:
        artifact_type: the registry artifact_type to regenerate.
        artifact_name: the live Weaviate class name (or the codegraph prefix
            sentinel — re-analyze rebuilds all 5 classes regardless).
        folder: the user-project folder (cwd for the project's scripts;
            ``knowledge/**`` source root).
        db_path: launcher.db (registry).
        project_id: project id for the registry row (None = orchestrator-wide,
            e.g. the shared KG).
        project_name: raw project name (for migrate-collections --name; the
            shared KG path doesn't need it).
        env: resolved env (passed to subprocesses; carries WEAVIATE_URL,
            SHARED_KG_COLLECTION, VCO_SHARED_KG_MIGRATE_CONSENT consent flag, …).
        weaviate_url: target Weaviate.
        when: materialized_at epoch-ms for the registry write (injected; the
            agent env has no wall clock).
        orchestrator_root: the orchestrator clone root (holds scripts/ +
            vco_lib). cwd for the shared-KG script + the ``-m vco_lib`` call.
        runner: subprocess runner (defaults to ``subprocess.run``); injectable
            so tests need no live Weaviate / shell.

    Returns:
        :class:`RegenerateResult`.
    """
    run = runner or _default_runner
    res = RegenerateResult(artifact_type=artifact_type, artifact_name=artifact_name)

    if artifact_type not in sv.CANONICAL_VERSIONS:
        res.error = f"unknown artifact_type {artifact_type!r}"
        return res

    sub_env = dict(env)
    sub_env.setdefault("WEAVIATE_URL", weaviate_url)

    try:
        if artifact_type == _SHARED_KG_TYPE:
            _regenerate_shared_kg(
                res, artifact_name=artifact_name, env=sub_env,
                orchestrator_root=orchestrator_root, run=run,
            )
        elif artifact_type in _PER_PROJECT_COLLECTION_TYPES:
            if not project_name:
                res.error = (
                    f"{artifact_type} regenerate requires the project name "
                    "(--name) to derive the canonical class names; none given"
                )
                return res
            _regenerate_per_project_collection(
                res, project_name=project_name, folder=folder, env=sub_env,
                weaviate_url=weaviate_url, orchestrator_root=orchestrator_root,
                run=run,
            )
        elif artifact_type == _CODEGRAPH_TYPE:
            _regenerate_codegraph(
                res, folder=folder, env=sub_env, run=run,
            )
        else:
            # A derived non-Weaviate-class type (e.g. a future vocabulary) has
            # no drop semantics here — refuse rather than guess.
            res.error = (
                f"{artifact_type} is not a regenerable Weaviate collection; "
                "no recreate action is defined for it"
            )
            return res
    except (OSError, subprocess.TimeoutExpired) as exc:
        res.error = f"regenerate subprocess failed: {type(exc).__name__}: {exc}"
        return res

    # Re-register at canonical ONLY when the drop+recreate actually completed.
    # A refusal (GUARD 1/2) or any error leaves the recorded version untouched
    # so the next update re-detects + re-prompts (the R3 retry posture).
    if res.dropped and not res.error and not res.refused:
        canonical = sv.canonical_version(artifact_type)
        # Idempotent unregister so a stale row is rewritten cleanly, then
        # register at canonical. register enforces schema_version==canonical.
        avr.unregister_artifact_version(
            db_path,
            project_id=project_id,
            artifact_type=artifact_type,
            artifact_name=artifact_name,
        )
        registered = avr.register_artifact_version(
            db_path,
            project_id=project_id,
            artifact_type=artifact_type,
            artifact_name=artifact_name,
            schema_version=canonical,
            materialized_at=when,
        )
        res.registered = bool(registered)
        if registered:
            res.ok = True
            if not res.detail:
                res.detail = (
                    f"regenerated {artifact_name} and registered at "
                    f"canonical v{canonical}"
                )
        else:
            # Recreate succeeded but the registry write failed (DB locked).
            # Do NOT claim ok — the next update will re-detect (stored row
            # absent → NEVER_MATERIALIZED → registered then).
            res.error = (
                "collection regenerated but the registry write failed "
                "(launcher.db locked/unwritable); the next update will "
                "record the version"
            )
    return res


# ---------------------------------------------------------------------------
# Per-type recreate composition (each reuses an existing machinery surface)
# ---------------------------------------------------------------------------


def _regenerate_shared_kg(
    res: RegenerateResult,
    *,
    artifact_name: str,
    env: dict,
    orchestrator_root: Path,
    run: Callable[..., subprocess.CompletedProcess],
) -> None:
    """Route the shared-KG regenerate through the EXISTING guarded script.

    The script is the only place GUARD 1 (kg-sync present) + GUARD 2
    (cross-project shared-write probe) live; reusing it means a consented
    recreate STILL refuses when cross-project shared nodes are unrecoverable.
    Exit 3 / 4 → ``refused`` (NOT dropped). Exit 0 → dropped + re-synced.
    """
    is_win = sys.platform.startswith("win")
    if is_win:
        script = orchestrator_root / "scripts" / "migrate-shared-kg-schema.ps1"
        cmd = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
        ]
    else:
        script = orchestrator_root / "scripts" / "migrate-shared-kg-schema.sh"
        cmd = ["bash", str(script)]

    if not script.is_file():
        res.error = (
            f"shared-KG migrate script not found at {script}; cannot "
            "regenerate (run install.py --update to materialize scripts/)"
        )
        return

    # The script reads SHARED_KG_COLLECTION; make sure the requested live
    # class name is what it operates on (don't trust an ambient value).
    script_env = dict(env)
    script_env["SHARED_KG_COLLECTION"] = artifact_name

    proc = run(
        cmd,
        cwd=str(orchestrator_root),
        env=script_env,
        timeout=_REGEN_TIMEOUT,
        capture_output=True,
        text=True,
    )
    stderr = proc.stderr or ""
    res.stderr_tail = _stderr_tail(stderr)
    rc = proc.returncode
    if rc == 0:
        res.dropped = True
        res.detail = (
            f"shared KG {artifact_name} dropped + recreated + re-synced "
            "via migrate-shared-kg-schema (GUARD 1/2 passed)"
        )
    elif rc in (3, 4):
        # GUARD refusal — NOTHING was dropped. Surface the reason verbatim.
        res.refused = True
        guard = "cross-project shared nodes unrecoverable" if rc == 3 else \
            "kg-sync resync helper not found"
        res.detail = (
            f"REFUSED ({guard}, exit {rc}). Nothing was dropped. "
            "To proceed anyway set VCO_SHARED_KG_MIGRATE_CONSENT=1 and "
            "retry (exit 3 only), or re-run each contributing project's "
            "kg-sync --all after migrating. See stderr for details."
        )
    else:
        res.error = (
            f"shared-KG regenerate exited rc={rc} (unexpected). The script "
            "no-ops + exits 0 on a missing collection / unreachable Weaviate, "
            "so a non-{0,3,4} code is an internal error; nothing recorded."
        )


def _regenerate_per_project_collection(
    res: RegenerateResult,
    *,
    project_name: str,
    folder: Path,
    env: dict,
    weaviate_url: str,
    orchestrator_root: Path,
    run: Callable[..., subprocess.CompletedProcess],
) -> None:
    """Route a per-project KG/Dev/Diagrams regenerate through the EXISTING
    ``migrate-collections --force-rebuild --project-folder`` CLI.

    ``--force-rebuild`` forces the ``rebuild`` action (drop+recreate with the
    canonical schema) and ``--project-folder`` triggers the CLI's post-rebuild
    re-ingest via ``.claude/scripts/sync_knowledge_graph.py --all`` (project_init
    .py:8488-8568). NO new drop path. We parse the JSON envelope to confirm the
    rebuild + re-ingest actually happened.
    """
    cmd = [
        sys.executable, "-m", "vco_lib.project_init", "migrate-collections",
        "--name", project_name,
        "--force-rebuild",
        "--project-folder", str(folder),
        "--weaviate-url", weaviate_url,
        "--json",
    ]
    proc = run(
        cmd,
        cwd=str(orchestrator_root),
        env=env,
        timeout=_REGEN_TIMEOUT,
        capture_output=True,
        text=True,
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    res.stderr_tail = _stderr_tail(stderr)
    if proc.returncode != 0:
        res.error = (
            f"migrate-collections --force-rebuild exited rc={proc.returncode}; "
            "the collection may not have been rebuilt. Nothing recorded."
        )
        return
    # Parse the envelope to confirm a rebuild ran + re-ingest succeeded.
    try:
        env_doc = json.loads(stdout)
    except (ValueError, json.JSONDecodeError):
        # rc==0 but no parseable JSON — treat conservatively as not-confirmed.
        res.error = (
            "migrate-collections returned no parseable JSON envelope; cannot "
            "confirm the rebuild completed. Nothing recorded."
        )
        return
    rebuilt = [
        e.get("collection") for e in env_doc.get("plan", [])
        if e.get("action") == "rebuild"
    ]
    reingest_required = bool(env_doc.get("reingest_required", False))
    errors = env_doc.get("errors") or []
    if errors:
        res.error = (
            f"migrate-collections reported {len(errors)} error(s): "
            f"{errors[0].get('error', '?')}. Nothing recorded."
        )
        return
    if reingest_required:
        res.error = (
            "rebuild dropped the collection but the re-ingest did not "
            "complete (reingest_required=true). Run `.claude/scripts/kg-sync "
            "--all` from the project folder, then re-run the update."
        )
        # Mark dropped so the caller knows the drop DID happen (data is on
        # disk, so this is recoverable) but do NOT register (re-ingest unproven).
        # C1: flag reingest_incomplete so the CLI handler writes a
        # schema_reingest_incomplete_<slug> deferral — otherwise the next
        # update sees the (now absent) registry row as NEVER_MATERIALIZED and
        # silently registers the EMPTY collection at canonical without ever
        # re-ingesting the on-disk source.
        res.dropped = True
        res.reingest_incomplete = True
        return
    res.dropped = True
    res.detail = (
        f"{res.artifact_name} dropped + rebuilt + re-synced from disk via "
        f"migrate-collections (rebuilt: {', '.join(rebuilt) or res.artifact_name})"
    )


def _regenerate_codegraph(
    res: RegenerateResult,
    *,
    folder: Path,
    env: dict,
    run: Callable[..., subprocess.CompletedProcess],
) -> None:
    """Route a codegraph regenerate through the EXISTING
    ``.claude/scripts/code-graph-analyze . --force-recreate`` (drop+rebuild the
    5 Code* classes from the source walk). NO new drop path.

    ``--force-recreate`` re-analyzes from scratch; the analyzer recreates the
    classes with the canonical schema. The 5 ``<prefix>_Code*`` classes share
    one recorded version so a single re-analyze re-derives all of them.

    NOTE (C2): analyze_code_graph.py defines ``--force-recreate``
    (analyze_code_graph.py:6832, plain ``parse_args`` with no
    ``allow_abbrev=False``). We pass it VERBATIM — NOT the ``--force``
    abbreviation, which works today only via argparse prefix-matching and would
    become ambiguous (argparse exit 2 → silent drop-stops-working) the moment
    any other ``--force*`` flag is added.
    """
    is_win = sys.platform.startswith("win")
    script = (
        folder / ".claude" / "scripts"
        / ("code-graph-analyze.ps1" if is_win else "code-graph-analyze")
    )
    if not script.is_file():
        res.error = (
            f"code-graph-analyze not found at {script}; cannot regenerate the "
            "codegraph collection (run install.py --update / Update bundle to "
            "materialize .claude/scripts/)"
        )
        return
    if is_win:
        cmd = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script), ".", "--force-recreate",
        ]
    else:
        cmd = ["bash", str(script), ".", "--force-recreate"]
    proc = run(
        cmd,
        cwd=str(folder),
        env=env,
        timeout=_REGEN_TIMEOUT,
        capture_output=True,
        text=True,
    )
    res.stderr_tail = _stderr_tail(proc.stderr or "")
    if proc.returncode != 0:
        res.error = (
            f"code-graph-analyze --force-recreate exited rc={proc.returncode}; "
            "the codegraph classes may not have been rebuilt. Nothing recorded."
        )
        return
    res.dropped = True
    res.detail = (
        "codegraph classes dropped + re-analyzed from the source walk via "
        "code-graph-analyze --force-recreate"
    )


def _default_runner(
    cmd: list[str], **kwargs
) -> subprocess.CompletedProcess:
    """Default subprocess runner. Isolated so tests inject a fake without
    spawning real processes / needing a live Weaviate + shell."""
    return subprocess.run(cmd, **kwargs)


# ---------------------------------------------------------------------------
# C1 — the re-ingest-incomplete deferral builder
# ---------------------------------------------------------------------------


def slugify_artifact_name(artifact_name: str, fallback: str) -> str:
    """Stable slug for a per-artifact ``condition_id`` suffix.

    Matches ``schema_migration_runner.build_deferral_entries``'s slug rule so
    the condition_id families stay consistent across surfaces.
    """
    return re.sub(r"[^A-Za-z0-9]+", "_", artifact_name).strip("_") or fallback


def _reingest_remediation_command(
    artifact_type: str, folder: Path, artifact_name: str
) -> str:
    """The exact command that completes the unfinished re-ingest for a
    dropped-but-empty collection (the C1 deferral's ``command_to_apply``)."""
    if artifact_type == "codegraph_collection":
        return (
            f"cd {str(folder)!r} && "
            ".claude/scripts/code-graph-analyze . --force-recreate"
        )
    # KG / Development / Diagrams (and the shared KG) re-ingest via kg-sync.
    return f"cd {str(folder)!r} && .claude/scripts/kg-sync --all"


def build_reingest_incomplete_entry(res: RegenerateResult, folder: Path):
    """Build the ``schema_reingest_incomplete_<slug>`` DeferralEntry for a
    regenerate that DROPPED the collection but could not confirm re-ingest
    (C1). Lazy-imports ``DeferralEntry`` so this module has no hard dependency
    on the deferral surface. Returns ``None`` when ``res`` is not a
    drop-without-reingest outcome (defensive).

    WHY this matters (C1): a dropped-but-not-reingested regenerate leaves the
    registry row absent. On the NEXT ``run_schema_migrations`` pass the artifact
    is seen as NEVER_MATERIALIZED and registered at canonical WITHOUT any
    re-ingest (the runner assumes the seed path materialized it) — silently
    marking an EMPTY Weaviate collection as current while the on-disk source is
    never re-embedded. This deferral makes the gap visible + actionable.
    """
    if not (res.reingest_incomplete and res.dropped and not res.ok):
        return None
    from .deferral_report import DeferralEntry

    slug = slugify_artifact_name(res.artifact_name, res.artifact_type)
    remediation = _reingest_remediation_command(
        res.artifact_type, folder, res.artifact_name
    )
    return DeferralEntry(
        condition_id=f"schema_reingest_incomplete_{slug}",
        severity="warning",
        title=f"Collection regenerated but not re-ingested: {res.artifact_name}",
        detected=(
            f"`{res.artifact_type}` (`{res.artifact_name}`) was dropped + "
            f"recreated during a Regenerate-now action, but the re-ingest from "
            f"disk did NOT complete ({res.error or 'reingest unconfirmed'}). The "
            f"collection is currently EMPTY on Weaviate. Its source is on disk, "
            f"so this is recoverable — but the schema version was deliberately "
            f"NOT recorded so a later run does not mistake the empty collection "
            f"for a freshly-seeded one."
        ),
        why_deferred=(
            "Re-ingesting can take minutes (re-embed) and may need a running "
            "Weaviate + embedding backend, so it is not retried inline. Until "
            "you run the command below, the collection stays empty; the next "
            "`install.py --update` will re-detect it as un-materialized and try "
            "again. This deferral exists so the empty state is never silently "
            "registered as current."
        ),
        command_to_apply=(
            "# Re-ingest the dropped collection from disk:\n"
            f"{remediation}\n"
            "# Then dismiss:\n"
            "python -m vco_lib.project_init dismiss-deferral "
            f"--folder {str(folder)!r} "
            f"--condition-id schema_reingest_incomplete_{slug}"
        ),
        kg_node_refs=[],
    )
