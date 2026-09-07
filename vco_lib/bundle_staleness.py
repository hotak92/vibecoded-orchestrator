# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Bundle-staleness census — is any registered project's bundle old? (WP-D).

The incident this module exists to prevent (WFT's D15): 12 of a real user's
13 projects carried bundles between 24 June and 10 August while the
orchestrator updated weekly, because **per-project bundle updates are manual
and nothing told anyone**. The launcher said "up to date" about the one thing
it never updates. This census is the missing "N projects have a bundle older
than the current version" sentence, and it is built so it can never lie in
that direction again.

Design rulings (R26 / R27 / the release's honesty thesis):

* **State-keyed, never version-keyed (R26).** A project is stale because a
  dry-run of the ONE bundle engine would change files on it — a pure
  function of currently-observed hashes — NOT because its recorded version
  is "older". A user who jumps six releases gets the same verdict as one who
  steps one. The recorded version is DISPLAY metadata (``recorded`` in the
  JSON), never the decision input.
* **The engine in dry-run is the census (R27).** ``install_project_bundle(
  ..., update_mode=True, dry_run=True)`` already walks the shipped set,
  applies the rewire transforms, and compares the manifest's recorded hashes
  against post-transform bytes via ``_file_action`` — the same classification
  an update would act on. CLI, GUI and doctor therefore cannot disagree with
  what an update would actually do; there is no second verdict implementation
  anywhere (the duplicated-verdict defect is how the launcher said "up to
  date" for five weeks).
* **Unknown is never current.** Every verdict is positively proven. A missing
  folder, a missing or unparseable manifest, an engine error, or an
  unavailable registry yields ``unknown`` with a ``reason`` — "could not
  determine" must never render as "fine". (A fresh root install before the
  first launcher boot legitimately reports ``registry="unavailable"``; the
  text output says so in as many words rather than printing "0 stale".)
* **One ledger lifecycle.** The census owns the ``project_bundles_stale``
  deferral entry end-to-end: a full census EMITS it when ``stale > 0`` OR
  ``unknown > 0`` and RESOLVES it only when both are ``0``
  (``paired-resolution``, the registry row names this module) — "unknown is
  never current" applies to the ledger too, so a census that determined
  nothing cannot clear an entry raised because bundles were stale. The
  entry names the two populations SEPARATELY (stale bundles; projects that
  could not be determined, with each one's reason) and carries the remedy
  for whichever is present, so it is cleared cause-by-cause rather than
  standing forever. The doctor probe and the CLI both go through
  :func:`run_census`, so the entry and the findings cannot fork.

Cost model: a full census is one read-only launcher.db query plus one
engine dry-run per project (filesystem + registry only — the census passes
the engine's ``_classification_only`` seam so it never probes Weaviate;
no writes anywhere — pinned by test). The per-project cost is measured by
test at ~hundreds of files; if that ever regresses past ~1 s/project the
census population, not the honesty, is what needs revisiting. The
in-engine post-update hook does NOT re-run the census: it flips the one
project it just proved current in the saved census state
(:func:`record_project_now_current`), so ``update_all_projects`` over N
projects costs N self-checks, not N² dry-runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

#: Registry condition owned by this module (row in
#: ``vco_lib/deferral_conditions.toml``; the completeness gate scans for this
#: literal, so the row must ship in the same change as this file).
CID_PROJECT_BUNDLES_STALE = "project_bundles_stale"

#: JSON schema version of both the CLI payload and the census state file.
CENSUS_SCHEMA = 1

#: Census state file, relative to the orchestrator root. Written ONLY by a
#: FULL census (never a ``--project``-filtered one): it is the cache the
#: in-engine flip updates, so it must always describe the whole population.
CENSUS_STATE_REL = Path(".claude") / "state" / "bundle-census.json"

#: Cap on ``changed_files`` listed per project in TEXT output. The JSON
#: payload carries the full list; only human terminals are capped.
TEXT_CHANGED_FILES_CAP = 50

#: The remediation command, exactly as the engine's own deferral texts print
#: it (verified against the ``install-bundle`` argparse at
#: ``vco_lib/project_init.py`` — ``--folder`` is a required named arg, never
#: a positional). A printed command is shipped code: keep the argv real.
REMEDY_CLI_TEMPLATE = (
    "python -m vco_lib.project_init install-bundle "
    "--folder <project-folder> --update --json"
)
REMEDY_GUI = "Projects → Update all bundles"

_VERDICT_CURRENT = "current"
_VERDICT_STALE = "stale"
_VERDICT_UNKNOWN = "unknown"

#: ``reason`` recorded on the census row of a project whose post-install
#: self-check found the bundle had NOT landed. The verdict stays the
#: existing ``unknown`` — "could not prove current" is exactly what it means.
REASON_SELF_CHECK_FAILED = "self_check_failed"

#: Engine action buckets whose presence means an update WOULD change files.
#: ``always-overwrite`` is deliberately ABSENT: the engine returns it for
#: every ``hooks/_lib`` file on EVERY run regardless of content, so counting
#: it would make every project stale forever. ``adopt`` and ``orphan-deleted``
#: change bytes/tree on update, so they count.
_STALE_ACTIONS = ("create", "overwrite", "adopt", "orphan-deleted")


# ---------------------------------------------------------------------------
# Census state (root-owned cache the in-engine flip updates)
# ---------------------------------------------------------------------------


def census_state_path(orchestrator_root: Path) -> Path:
    """Absolute path of the census state file under ``orchestrator_root``."""
    return Path(orchestrator_root) / CENSUS_STATE_REL


def read_census_state(orchestrator_root: Path) -> Optional[dict]:
    """Load the last full census's state, or ``None`` (absent/unparseable).

    ``None`` simply means "no census has completed since the state file was
    last cleared" — callers skip the ledger refresh, they never guess.
    """
    path = census_state_path(orchestrator_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != CENSUS_SCHEMA:
        return None
    if not isinstance(data.get("projects"), list):
        return None
    return data


def _write_census_state(orchestrator_root: Path, payload: dict) -> bool:
    """Best-effort atomic write of the census state. Returns success."""
    path = census_state_path(orchestrator_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        return True
    except OSError:
        return False


def _summary_of(projects: Sequence[dict]) -> dict:
    counts = {_VERDICT_CURRENT: 0, _VERDICT_STALE: 0, _VERDICT_UNKNOWN: 0}
    for row in projects:
        verdict = row.get("verdict")
        if verdict in counts:
            counts[verdict] += 1
        else:  # pragma: no cover — defensive: rows are built by this module
            counts[_VERDICT_UNKNOWN] += 1
    return counts


# ---------------------------------------------------------------------------
# Classification — the ONE decision, per project
# ---------------------------------------------------------------------------


def _empty_counts() -> dict:
    return {"created": 0, "overwritten": 0, "preserved": 0, "noop": 0}


def _project_row(
    ref_id: str,
    name: str,
    folder: str,
    verdict: str,
    reason: str,
    *,
    recorded: Optional[dict] = None,
    counts: Optional[dict] = None,
    changed_files: Optional[list] = None,
    user_modified: int = 0,
) -> dict:
    """Build one §5.1-shaped project row."""
    return {
        "id": ref_id,
        "name": name,
        "folder": folder,
        "verdict": verdict,
        "reason": reason,
        "recorded": recorded
        or {"version": None, "commit": None, "installed_at": None},
        "counts": counts or _empty_counts(),
        "changed_files": changed_files or [],
        "user_modified": user_modified,
    }


def _classify_registered_project(
    root: Path, ref, running: dict
) -> dict:
    """Verdict one registered project. Never raises; never writes.

    Decision order (each unknown short-circuits — later steps need the
    earlier ones' preconditions):

    1. folder on disk?        → else ``unknown/folder_missing``
    2. manifest present?      → else ``unknown/manifest_missing``
    3. manifest parseable?    → else ``unknown/manifest_unparseable``
    4. engine dry-run clean?  → else ``unknown/engine_error``
    5. classify the actions   → ``stale`` / ``current``
    """
    folder = Path(ref.folder).resolve()
    if not folder.is_dir():
        return _project_row(
            ref.id, ref.name, str(folder), _VERDICT_UNKNOWN, "folder_missing"
        )

    manifest_path = folder / ".claude" / ".vco-manifest.json"
    if not manifest_path.is_file():
        return _project_row(
            ref.id, ref.name, str(folder), _VERDICT_UNKNOWN, "manifest_missing"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest is not a JSON object")
    except (OSError, ValueError):
        return _project_row(
            ref.id, ref.name, str(folder),
            _VERDICT_UNKNOWN, "manifest_unparseable",
        )

    from vco_lib import vco_version as _vv

    version, commit = _vv.recorded_manifest_version(manifest)
    recorded = {
        "version": version,
        "commit": commit,
        "installed_at": manifest.get("installed_at")
        if isinstance(manifest.get("installed_at"), str)
        else None,
    }

    # The census IS the engine in dry-run (see module docstring). Lazy import:
    # project_init is a 16k-line module and CLI startup should not pay for it
    # when the registry is unavailable anyway.
    from vco_lib.project_init import install_project_bundle

    try:
        result = install_project_bundle(
            folder,
            orchestrator_root=root,
            update_mode=True,
            dry_run=True,
            # safe_add is a CREATE-mode, per-request flag (never persisted on
            # the projects row; update_project_v2 does not pass it) and is
            # inert under update_mode+dry_run regardless — False is the value
            # the real update flow runs with.
            safe_add=False,
            # Classification re-entry: skip the residue/foreign-row step —
            # the census is filesystem-only (no Weaviate dependency, no
            # double-invocation of a real install's wiring).
            _classification_only=True,
        )
    except Exception as exc:  # noqa: BLE001 — census never raises
        return _project_row(
            ref.id, ref.name, str(folder), _VERDICT_UNKNOWN, "engine_error",
            recorded=recorded,
            changed_files=[f"{type(exc).__name__}: {exc}"],
        )

    if result.get("errors"):
        first = result["errors"][0]
        detail = first.get("error", "?") if isinstance(first, dict) else str(first)
        return _project_row(
            ref.id, ref.name, str(folder), _VERDICT_UNKNOWN, "engine_error",
            recorded=recorded,
            changed_files=[str(detail)],
        )

    actions = result.get("actions") or {}
    changed: list[str] = []
    for key in _STALE_ACTIONS:
        changed.extend(actions.get(key) or [])
    user_modified = len(actions.get("preserve") or []) + len(
        actions.get("orphan-preserved") or []
    )
    counts = _empty_counts()
    counts["created"] = len(actions.get("create") or [])
    counts["overwritten"] = (
        len(actions.get("overwrite") or [])
        + len(actions.get("adopt") or [])
        + len(actions.get("orphan-deleted") or [])
    )
    counts["preserved"] = len(actions.get("preserve") or []) + len(
        actions.get("orphan-preserved") or []
    )
    counts["noop"] = len(actions.get("noop") or [])

    if changed:
        return _project_row(
            ref.id, ref.name, str(folder), _VERDICT_STALE, "files_changed",
            recorded=recorded, counts=counts,
            changed_files=sorted(changed), user_modified=user_modified,
        )
    return _project_row(
        ref.id, ref.name, str(folder), _VERDICT_CURRENT, "noop",
        recorded=recorded, counts=counts, user_modified=user_modified,
    )


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------


def run_census(
    orchestrator_root: Optional[Path] = None,
    *,
    project_filter: Optional[str] = None,
    refresh_ledger: bool = True,
    write_state: bool = True,
) -> dict:
    """Run the census and return the §5.1 JSON payload. Never raises.

    Args:
        orchestrator_root: the orchestrator clone/tarball root. ``None`` →
            resolved the way ``project_init``'s CLI does (walk up from this
            module for ``vct-module.json``).
        project_filter: restrict to ONE project by launcher id or folder
            path. A filtered run REPORTS ONLY: it writes no census state and
            touches no ledger, because a one-project summary must never be
            mistaken for the population's.
        refresh_ledger: emit/resolve the root ``project_bundles_stale``
            entry. The doctor probe and the CLI's text run leave this on.
            Tests turn it off to observe verdicts in isolation.
        write_state: persist the result as the root's census state
            (``.claude/state/bundle-census.json``). Off + ``refresh_ledger``
            off is a fully READ-ONLY census — what ``--json`` runs, so a
            caller that only wants to READ the population's verdicts cannot
            mutate root state as a side effect of asking.

    A full census also persists its result as the root's census state — the
    cache :func:`record_project_now_current` flips, so a bundle update never
    has to re-census every OTHER project to keep the ledger count honest.
    """
    root = Path(
        orchestrator_root
        if orchestrator_root is not None
        else _default_orchestrator_root()
    )
    from vco_lib import vco_version as _vv

    running_version = _vv.resolve(root)
    running = {
        "version": running_version.semver,
        "commit": running_version.commit,
    }

    from vco_lib import launcher_db_reader as _ldr

    refs = _ldr.list_registered_projects()
    if refs is None:
        rows: list[dict] = []
        registry = "unavailable"
        summary = {_VERDICT_CURRENT: 0, _VERDICT_STALE: 0, _VERDICT_UNKNOWN: 0}
    else:
        registry = "launcher.db"
        if project_filter:
            refs = [r for r in refs if _ref_matches(r, project_filter)]
        rows = [_classify_registered_project(root, r, running) for r in refs]
        summary = _summary_of(rows)

    payload = {
        "schema": CENSUS_SCHEMA,
        "running": running,
        "registry": registry,
        "orchestrator_root": str(root),
        "projects": rows,
        "summary": summary,
        "remedy": {"gui": REMEDY_GUI, "cli": REMEDY_CLI_TEMPLATE},
    }

    if project_filter is None and registry == "launcher.db":
        state = dict(payload)
        state["census_at"] = _now_iso()
        if write_state:
            _write_census_state(root, state)
        if refresh_ledger:
            refresh_root_ledger(root, state)
    return payload


def _ref_matches(ref, needle: str) -> bool:
    """Does this registry row match a ``--project`` id-or-path filter?"""
    if ref.id == needle or ref.folder == needle:
        return True
    try:
        return Path(ref.folder).resolve() == Path(needle).resolve()
    except OSError:  # pragma: no cover — resolve on odd strings
        return False


def _default_orchestrator_root() -> Path:
    """Resolve the root the way ``project_init``'s CLI does — via ITS walker,
    so there is exactly one definition of "what is the orchestrator root"."""
    from vco_lib.project_init import _find_orchestrator_root_from_module

    return _find_orchestrator_root_from_module()


# ---------------------------------------------------------------------------
# Ledger lifecycle (paired-resolution, owned HERE)
# ---------------------------------------------------------------------------


def _stale_names(rows: Sequence[dict]) -> str:
    """``Name (N file(s))`` for every stale row, comma-joined."""
    return ", ".join(
        f"{r.get('name') or '?'} ({len(r.get('changed_files') or [])} file(s))"
        for r in rows
    )


def _unknown_names(rows: Sequence[dict]) -> str:
    """``Name (reason)`` for every undetermined row, comma-joined.

    The per-project reason is what makes the entry ACTIONABLE rather than a
    permanent badge: "Old Client (folder_missing)" tells the user which row
    to fix or remove, where "2 could not be determined" tells them nothing.
    """
    return ", ".join(
        f"{r.get('name') or '?'} ({r.get('reason') or 'unknown'})"
        for r in rows
    )


#: What to DO about each ``unknown`` reason. Rendered into the entry's
#: command block whenever undetermined projects are present, so the user can
#: clear the entry cause-by-cause instead of staring at an unclearable badge.
UNKNOWN_REASON_REMEDIES: tuple[tuple[str, str], ...] = (
    ("folder_missing",
     "the folder is gone — restore it, or remove the project from the "
     "launcher (Projects → remove)"),
    ("manifest_missing",
     "never bundled — install one: python -m vco_lib.project_init "
     "install-bundle --folder <project-folder>"),
    ("manifest_unparseable",
     "repair <project-folder>/.claude/.vco-manifest.json, or re-install the "
     "bundle with --update --force"),
    (REASON_SELF_CHECK_FAILED,
     "the last bundle update did NOT land (locked/unwritable file?) — re-run "
     "it and read its warnings"),
    ("engine_error",
     "see the per-project detail in `python -m vco_lib.bundle_staleness "
     "--json`"),
)


def build_stale_entry(census: dict) -> object:
    """The ``project_bundles_stale`` DeferralEntry for a census.

    Renders whichever of the TWO populations is non-empty, separately and by
    name — stale bundles, and projects VCO could not determine. They are
    different facts with different remedies, and collapsing them is how the
    entry starts lying: once ``stale == 0`` and only ``unknown`` remains,
    "N project bundles are older than the orchestrator" is simply false,
    and "run Update all bundles" fixes nothing (those bundles are not stale;
    VCO could not read them at all). An entry that names the cause per
    project is clearable cause-by-cause; one that does not is a permanent
    badge — the failure mode of a strict never-resolve-on-unknown rule.

    Returned as a plain ``object`` to keep this module import-light; the
    value is a real ``vco_lib.deferral_report.DeferralEntry`` (same
    duck-typing convention as ``codegraph_extractor_generation``'s
    ``ReindexResult.deferral``).
    """
    from vco_lib.deferral_report import DeferralEntry

    summary = census.get("summary") or {}
    stale = int(summary.get(_VERDICT_STALE) or 0)
    unknown = int(summary.get(_VERDICT_UNKNOWN) or 0)
    rows = census.get("projects") or []
    stale_rows = [r for r in rows if r.get("verdict") == _VERDICT_STALE]
    unknown_rows = [r for r in rows if r.get("verdict") == _VERDICT_UNKNOWN]
    total = len(rows)
    running = census.get("running") or {}
    version = running.get("version") or "unknown"

    sentences: list[str] = []
    if stale:
        sentences.append(
            f"{stale} of {total} registered project(s) are on a stale "
            f"bundle — older than the current orchestrator version "
            f"({version}): {_stale_names(stale_rows)}."
        )
        sentences.append(
            "Per-project bundle updates are manual: an orchestrator update"
            " never touches them, and nothing else reports the gap."
        )
    if unknown:
        sentences.append(
            f"{unknown} of {total} registered project(s) could NOT be "
            f"determined — VCO has no verdict for them, which is not the "
            f"same as 'current': {_unknown_names(unknown_rows)}."
        )

    if stale and unknown:
        title = (
            f"{stale} project bundle(s) stale, {unknown} undetermined"
        )
    elif stale:
        title = f"{stale} project bundle(s) older than the orchestrator"
    else:
        title = f"{unknown} project(s) VCO could not determine"

    commands: list[str] = []
    if stale:
        commands.extend([
            "# Update every stale bundle at once (launcher GUI):",
            f"#   {REMEDY_GUI}",
            "# Or one project from a shell:",
            REMEDY_CLI_TEMPLATE,
        ])
    if unknown:
        if commands:
            commands.append("")
        commands.append(
            "# Undetermined projects — these bundles are NOT known to be "
            "stale;"
        )
        commands.append(
            "# resolve each named cause, then re-run the census below:"
        )
        commands.extend(
            f"#   {reason} → {advice}"
            for reason, advice in UNKNOWN_REASON_REMEDIES
        )
        commands.append("python -m vco_lib.bundle_staleness")

    return DeferralEntry(
        condition_id=CID_PROJECT_BUNDLES_STALE,
        title=title,
        detected=" ".join(sentences),
        why_deferred=(
            "Updating a user project's bundle writes into that project's "
            "working tree (overwriting stale VCO-shipped files, preserving "
            "user-modified ones), so VCO never batch-updates every project "
            "unattended from a root install. The census re-runs at the end "
            "of every root install/update and after every project bundle "
            "update; this entry clears the first census in which every "
            "registered project is accounted for — nothing stale AND "
            "nothing undetermined. An undetermined project never counts as "
            "current, so clear those by fixing or removing the named "
            "registry row (see the commands below)."
        ),
        command_to_apply="\n".join(commands),
        severity="warning",
    )


def refresh_root_ledger(orchestrator_root: Path, census: dict) -> str:
    """Emit-or-resolve ``project_bundles_stale`` in the ROOT ledger.

    ONE home for the entry's lifecycle: ``stale > 0 OR unknown > 0`` →
    (re)emit, with the text rebuilt from THIS census; ``stale == 0 AND
    unknown == 0`` → resolve. Called by full censuses and by the in-engine
    flip. Writes go through the locked emitter (never a raw
    read-modify-write), which also keeps install.py's ``finalize``
    late-merge correct: the entry is foreign to install.py, so its seeded
    copy is preserved — and dropped by the vanish-reconcile when this path
    resolves it mid-run.

    **Unknown never resolves** (same rule as "unknown is never current"): a
    census that could not determine a project has not proven that project
    current, so clearing on it would be the module's own honesty thesis
    inverted — an all-``unknown`` census would silently declare the
    population healthy. The entry does not become IMMORTAL as a result,
    because the re-emit re-renders it: once nothing is stale it names the
    undetermined projects and their per-project reasons, and its command
    block says how to clear each cause (fix or remove the registry row).
    "Cannot ever clear" would be a permanent badge; "clear by resolving
    each named cause" is an action.

    Returns ``"emitted"`` / ``"resolved"`` / ``"untouched"`` (nothing owed
    and nothing to remove) / ``"error:<msg>"`` (soft-fail, never raises).
    """
    root = Path(orchestrator_root)
    summary = census.get("summary") or {}
    stale = int(summary.get(_VERDICT_STALE) or 0)
    unknown = int(summary.get(_VERDICT_UNKNOWN) or 0)
    try:
        from vco_lib import deferral_emit as _de

        if stale > 0 or unknown > 0:
            # RE-emitted (not merely left standing) on the unknown-only
            # population too: the entry's text is rebuilt from the CURRENT
            # census every time, so a report whose stale projects have all
            # been healed stops claiming they are stale and starts naming
            # the undetermined ones and their causes instead.
            _de.emit(root, build_stale_entry(census))  # type: ignore[arg-type]
            return "emitted"
        removed = _de.resolve_conditions(root, (CID_PROJECT_BUNDLES_STALE,))
        return "resolved" if removed else "untouched"
    except Exception as exc:  # noqa: BLE001 — ledger I/O never breaks a census
        return f"error:{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# The in-engine post-update hook (project install / project update surface)
# ---------------------------------------------------------------------------


def self_check(
    folder: Path,
    orchestrator_root: Path,
    *,
    update_mode: bool,
    manifest_written: bool,
    skip_kinds: frozenset = frozenset(),
) -> list[str]:
    """Prove the bundle that just installed actually landed. Returns warnings.

    Two positive checks, both cheap (one manifest read + one dry-run of the
    same engine):

    1. The manifest just written records the RUNNING semver (when the
       running semver is known). A mismatch means the manifest write and the
       install disagreed — the "said it updated, didn't" failure shape.
    2. A second dry-run of the SAME engine in the SAME mode finds nothing
       left to create/overwrite/adopt/orphan-delete. ``always-overwrite``
       and user-preserved files are exempt (they legitimately re-report).

    A fresh-install run is checked in fresh-install mode, where a
    pre-existing divergent file legitimately classifies ``skip-existing`` —
    an update-mode re-run would false-alarm on exactly those files.
    ``skip_kinds`` mirrors the run's own scope: a run that deliberately
    skipped a kind must not be flagged for the files it chose not to ship.
    """
    folder = Path(folder)
    root = Path(orchestrator_root)
    warnings: list[str] = []

    from vco_lib import vco_version as _vv

    running = _vv.resolve(root)
    if manifest_written and running.semver is not None:
        try:
            manifest = json.loads(
                (folder / ".claude" / ".vco-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            recorded, _commit = _vv.recorded_manifest_version(manifest)
        except (OSError, ValueError):
            recorded = None
        if recorded != running.semver:
            warnings.append(
                "bundle self-check: manifest records "
                f"vco_version={recorded!r} but the running orchestrator is "
                f"{running.semver!r} — the install may not have landed"
            )

    from vco_lib.project_init import install_project_bundle

    try:
        recheck = install_project_bundle(
            folder,
            orchestrator_root=root,
            update_mode=update_mode,
            dry_run=True,
            safe_add=False,
            skip_kinds=frozenset(skip_kinds),
            # Classification re-entry — see _classify_registered_project.
            _classification_only=True,
        )
    except Exception as exc:  # noqa: BLE001 — self-check never breaks install
        warnings.append(f"bundle self-check could not run: {exc}")
        return warnings

    actions = recheck.get("actions") or {}
    pending = []
    if update_mode:
        pending = [
            *sorted(actions.get("create") or []),
            *sorted(actions.get("overwrite") or []),
            *sorted(actions.get("adopt") or []),
            *sorted(actions.get("orphan-deleted") or []),
        ]
    else:
        pending = sorted(actions.get("create") or [])
    if pending:
        warnings.append(
            "bundle self-check: "
            f"{len(pending)} file(s) still differ from the shipped bundle "
            "after the install (e.g. "
            + ", ".join(pending[:3])
            + (", …" if len(pending) > 3 else "")
            + ") — re-run the bundle update or see UPDATE_DEFERRED.md"
        )
    return warnings


def record_project_now_current(
    orchestrator_root: Path, folder: Path
) -> bool:
    """Flip one project to ``current`` in the census state + refresh ledger.

    Called at the end of a REAL (non-dry-run) ``install_project_bundle`` for
    the project that just installed — which is current **only once**
    :func:`self_check` has passed (see :func:`post_install_hook`, which owns
    that precondition; a failed self-check routes to
    :func:`record_project_unknown` instead). This is the incremental half of
    the census: ``update_all_projects`` over N projects costs N flips, not N
    full censuses. The ledger entry this refreshes is about the OTHER
    projects (this one is excluded from the stale set by the flip itself).

    No census state on disk (census never ran here) → no-op, return
    ``False``: nothing is invented. Never raises.

    Concurrency note: the state file's read-modify-write is not locked
    (the LEDGER half is, via the deferral lock). ``update_all_projects``
    runs projects sequentially, so the normal case is race-free; a lost
    flip under a hand-rolled parallel update is self-healing — the next
    full census re-measures every project from observed state.
    """
    return _record_project_verdict(
        orchestrator_root, folder, _VERDICT_CURRENT, "noop"
    )


def record_project_unknown(
    orchestrator_root: Path, folder: Path, reason: str
) -> bool:
    """Mark one project ``unknown`` in the census state + refresh ledger.

    The counterpart of :func:`record_project_now_current` for the case the
    install could NOT prove: an install whose self-check found files still
    differing has not shown the bundle landed, so the census must not read
    ``current`` for it. ``unknown`` is the module's existing "not positively
    proven" verdict — no new state is introduced.
    """
    return _record_project_verdict(
        orchestrator_root, folder, _VERDICT_UNKNOWN, reason
    )


def _record_project_verdict(
    orchestrator_root: Path, folder: Path, verdict: str, reason: str
) -> bool:
    """Rewrite one project's row in the saved census state, then refresh the
    root ledger from the recomputed summary. Returns whether a row matched.

    ONE home for the in-engine flip (both verdicts go through here) so the
    state-file shape, the summary recomputation and the ledger refresh
    cannot drift between them.
    """
    root = Path(orchestrator_root)
    state = read_census_state(root)
    if state is None:
        return False
    try:
        target = Path(folder).resolve()
    except OSError:  # pragma: no cover — resolve on odd paths
        return False
    rows = state.get("projects") or []
    flipped = False
    for row in rows:
        try:
            row_folder = Path(str(row.get("folder"))).resolve()
        except OSError:  # pragma: no cover
            continue
        if row_folder != target:
            continue
        row["verdict"] = verdict
        row["reason"] = reason
        row["counts"] = _empty_counts()
        row["changed_files"] = []
        row["user_modified"] = 0
        flipped = True
    if not flipped:
        return False
    state["summary"] = _summary_of(rows)
    if not _write_census_state(root, state):
        return False
    refresh_root_ledger(root, state)
    return True


def post_install_hook(
    folder: Path,
    orchestrator_root: Path,
    *,
    update_mode: bool,
    manifest_written: bool,
    skip_kinds: frozenset,
    log=None,
) -> list[str]:
    """The engine's thin post-install step: self-check + census flip.

    Called at the end of every REAL (non-dry-run)
    ``install_project_bundle`` run (all four R27 surfaces reach the engine).
    Returns the warnings to append to the result envelope; never raises —
    a failed self-check must never fail the install it is diagnosing.
    Logs its phases via ``log`` when given (the engine's forensic logger).

    **The self-check GATES the flip.** ``self_check`` reports rather than
    raises, so its warnings are the only signal that the bundle did not
    land (a shipped destination locked by AV/OneDrive, an unwritable path,
    a manifest that records a different version than the run). Flipping to
    ``current`` regardless would leave the census — and the root ledger
    entry it drives — saying "updated" about an install that demonstrably
    did not update: the exact defect this census exists to end. A
    self-check that produced warnings therefore records ``unknown`` with
    ``reason="self_check_failed"``, never ``current``.
    """
    warnings: list[str] = []
    try:
        check_warnings = self_check(
            folder,
            orchestrator_root,
            update_mode=update_mode,
            manifest_written=manifest_written,
            skip_kinds=skip_kinds,
        )
        warnings.extend(check_warnings)
        if check_warnings:
            verdict = _VERDICT_UNKNOWN
            flipped = record_project_unknown(
                orchestrator_root, folder, REASON_SELF_CHECK_FAILED
            )
        else:
            verdict = _VERDICT_CURRENT
            flipped = record_project_now_current(orchestrator_root, folder)
        if log is not None:
            try:
                log("4.bundle.selfcheck", "end",
                    "bundle self-check complete",
                    data={"flipped_census_row": flipped,
                          "verdict": verdict})
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — never blocks the install
        warnings.append(f"post-install self-check failed: {exc}")
    return warnings


# ---------------------------------------------------------------------------
# Rendering + CLI
# ---------------------------------------------------------------------------


def render_text(payload: dict) -> str:
    """Human rendering. Honest by construction: all three counts always
    print, an unavailable registry says so instead of "0 stale", and
    per-project ``changed_files`` are capped (JSON carries the full list)."""
    lines: list[str] = []
    summary = payload.get("summary") or {}
    registry = payload.get("registry")
    running = payload.get("running") or {}
    lines.append(
        "bundle census — running "
        f"{running.get('version') or 'unknown'}"
        + (f" ({running.get('commit')})" if running.get("commit") else "")
    )
    if registry == "unavailable":
        lines.append(
            "registry unavailable (launcher.db not found or unreadable) — "
            "no project's bundle state could be determined. This is normal "
            "on a fresh root install before the first launcher boot."
        )
    lines.append(
        "projects: {c} current, {s} stale, {u} unknown".format(
            c=summary.get(_VERDICT_CURRENT, 0),
            s=summary.get(_VERDICT_STALE, 0),
            u=summary.get(_VERDICT_UNKNOWN, 0),
        )
    )
    for row in payload.get("projects") or []:
        verdict = row.get("verdict")
        name = row.get("name") or "?"
        reason = row.get("reason") or "?"
        marker = {"stale": "!", "unknown": "?", "current": "ok"}.get(
            verdict, "?"
        )
        lines.append(f"  [{marker}] {name} — {verdict} ({reason})")
        if verdict == _VERDICT_STALE:
            changed = row.get("changed_files") or []
            for rel in changed[:TEXT_CHANGED_FILES_CAP]:
                lines.append(f"        {rel}")
            if len(changed) > TEXT_CHANGED_FILES_CAP:
                lines.append(
                    f"        … and {len(changed) - TEXT_CHANGED_FILES_CAP}"
                    " more (full list in --json)"
                )
    stale = int(summary.get(_VERDICT_STALE) or 0)
    if stale:
        remedy = payload.get("remedy") or {}
        lines.append(f"remedy: {remedy.get('gui') or REMEDY_GUI}")
        lines.append(f"        {remedy.get('cli') or REMEDY_CLI_TEMPLATE}")
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m vco_lib.bundle_staleness [--json] [--project <id|path>]``.

    Exit codes: ``0`` — the census ran (verdicts may include stale/unknown);
    ``2`` — the census could not run at all (no orchestrator root). Never
    exits non-zero for FINDING staleness: a diagnostic that fails the
    command on what it found trains users to stop running it.
    """
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.bundle_staleness",
        description=(
            "Census of registered projects' bundle staleness against the "
            "running orchestrator. Verdicts are state-keyed (would a bundle "
            "update change files?), never version-deltas."
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit the machine payload (schema pinned in the wave-3 plan "
        "§5.1). READ-ONLY: no census state file and no ledger write unless "
        "--refresh-ledger is also given",
    )
    parser.add_argument(
        "--refresh-ledger", action="store_true",
        help="with --json: also persist the census state and emit/resolve "
        "the root project_bundles_stale entry (the text run's default). "
        "Ignored with --project, which is always report-only",
    )
    parser.add_argument(
        "--project", default=None,
        help="restrict to ONE project (launcher id or folder path); "
        "report-only — no census state or ledger writes",
    )
    parser.add_argument(
        "--orchestrator-root", default=None,
        help="override the orchestrator root (tests + unusual layouts)",
    )
    args = parser.parse_args(argv)

    root = (
        Path(args.orchestrator_root)
        if args.orchestrator_root
        else _default_orchestrator_root()
    )
    if not root.is_dir():
        print(
            f"error: orchestrator root not found: {root}",
            file=sys.stderr,
        )
        return 2
    # A `--json` run is a QUERY: something asked what the population looks
    # like. Answering must not emit/resolve a root deferral or rewrite the
    # census state — a read that mutates root state cannot be used by a GUI
    # poll, a script, or a human checking twice. The text run keeps the
    # refresh (it IS the user-facing census), and `--refresh-ledger` is the
    # explicit opt-in for a machine caller that wants both.
    persist = args.refresh_ledger or not args.json
    payload = run_census(
        root, project_filter=args.project,
        refresh_ledger=persist, write_state=persist,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(payload))
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    raise SystemExit(main())
