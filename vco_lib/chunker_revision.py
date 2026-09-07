# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-project chunker-revision sentinel lifecycle (v0.2.92 WP-D).

The state-keyed chunker-resync gate for ONE project — the per-project port
of the launcher's root-only, GUI-boot-only R2-4 flow
(``chunker_revision_deferral.rs::write_chunker_deferral_if_revision_changed``)
into the ONE bundle engine, so all four install/update surfaces
(root install / root update / project install / project update) get it
(R27). Extracted from ``project_init`` (which is ratchet-capped and must
shrink, not grow): the engine keeps a thin call site only.

Why this module exists as the fix for a specific defect: the gate that DID
run per-project before v0.2.92 was ``_crosses_chunker_boundary``, which
compared the manifest's ``vco_version`` — a git short SHA on every clone
install, never semver — against a semver constant. That comparison returns
``False`` on unparseable input, so it never fired for any real user, no
matter how they updated (WP-A skip-safety map, row 2). Per-project bundle
installs had ZERO state-keyed chunker-resync detection. This gate keys on
OBSERVED STATE (R26): the project's stored last-seen ``_CHUNKER_REVISION``
sentinel vs the live one. How many releases were skipped is irrelevant —
anyone whose stored revision differs from the current one gets the resync
deferral, exactly as if they had stepped one release at a time.

v0.2.92 round-5 MAJOR-R5-4 closed a hole in the fresh-install arm: a
re-cloned / manifest-deleted project has neither sentinel nor manifest,
so it read as "fresh" while its Weaviate collection kept the older
chunking — and the by-hand remedy the old KNOWN_ISSUES entry printed
(``kg-sync --all``) re-chunked nothing, because the sync script arms its
plan comparison only while the ledger carries the crossing entry such a
project never receives. Two fixes, one per side: the gate's fresh arm now
consults the project's REGISTERED bound KG class (objects > 0 → the
resync IS owed; count unknown → no stamp, the next run re-decides), and
``sync_knowledge_graph.py --rechunk`` arms the plan comparison without a
ledger entry so the by-hand remedy is TRUE for every population.

The state lives in the project's OWN ``.claude/state/`` (not launcher.db):
the engine runs for CLI-only and never-booted-the-GUI installs too, and
per-project state stays genuinely per-project (the KG/code-graph isolation
model). The launcher's root flow keeps its global ``app_state`` sentinel
unchanged; both write the same ``chunker_preset_overhaul_pending``
condition, so their ledgers dedup by last-write-wins.

Read-only + one small JSON stamp per install. Never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

#: Per-project last-seen chunker revision, relative to the project folder.
STATE_REL = Path(".claude") / "state" / "chunker-revision.json"


def state_path(folder: Path) -> Path:
    """Absolute path of the sentinel state file under ``folder``."""
    return Path(folder) / STATE_REL


def read_last_revision(folder: Path) -> Optional[str]:
    """The project's last-seen ``_CHUNKER_REVISION``, or ``None``.

    ``None`` covers absent/unreadable/unparseable/shape-mismatch — "never
    observed" and "could not read" both arm nothing and stamp nothing,
    which is the conservative arm (no resync is owed on a first
    observation; the next successful read compares for real).
    """
    try:
        data = json.loads(
            state_path(folder).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    revision = data.get("revision") if isinstance(data, dict) else None
    return revision if isinstance(revision, str) and revision.strip() else None


def write_last_revision(folder: Path, revision: str) -> bool:
    """Best-effort atomic stamp of the project's last-seen revision.

    Never raises; returns success so the caller can log (never gate on it —
    a failed stamp just means the next run re-compares).
    """
    try:
        target = state_path(folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"revision": revision, "updated_at": _now_iso()}
            ) + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
        return True
    except OSError:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resync_commands(folder: Path, tail_comment: str) -> str:
    """The ONE re-chunk remediation both chunker deferrals emit.

    Two emitters exist in ``project_init`` (the v0.2.46 SEMVER-boundary gate
    and the ``_CHUNKER_REVISION`` gate) and they share a ``condition_id``, so
    their commands MUST be the same text — a divergence would ship whichever
    ran first. One builder, two callers. It lives here rather than in
    ``project_init`` because this module owns the chunker-resync lifecycle and
    ``project_init`` is ratchet-capped (its call sites stay thin).

    Shape (v0.2.92, BLOCKER-2 + the R42 portability sweep):

    * ``--from-resolver`` on the analyzer. ``--force-recreate`` DROPS the five
      ``<prefix>_Code*`` classes, and without an identity flag the analyzer's
      last resolution rung is the folder BASENAME (``.claude/env`` is
      shell-sourced for hooks, not for the terminal the user pastes this into)
      — which rebuilds the wrong family for any moved/renamed project and, on
      a basename collision, drops ANOTHER project's code graph. This is the
      flag the per-edit hooks and the launcher already use;
      ``vco_lib.codegraph_drop_guard`` is the run-time backstop for the case
      where the hub is down and ``--from-resolver`` itself falls back.
    * Absolute wrapper paths via :mod:`vco_lib.remedy_shell` instead of
      ``cd <folder>`` + relative paths. The old form was POSIX-only three
      times over (``cd`` with an unquoted path, a ``.claude/scripts/`` wrapper
      that is not directly executable on Windows, and an unstated "you must be
      in the project directory" precondition). The analyzer takes the folder
      as its positional ``repo_path``, so no ``cd`` is needed at all; kg-sync
      derives its root from its own location.
    """
    from vco_lib import remedy_shell

    kg = remedy_shell.script_invocation(
        folder, ".claude/scripts/kg-sync", "--all",
    )
    cg = remedy_shell.script_invocation(
        folder, ".claude/scripts/code-graph-analyze",
        remedy_shell.quote(str(folder)), "--from-resolver", "--force-recreate",
    )
    return (
        "# Re-chunk this project's KG:\n"
        f"{kg}\n"
        "\n"
        "# Re-chunk this project's code graph (drop + rebuild the 5 Code*\n"
        "# classes so every entity re-embeds). --from-resolver makes the\n"
        "# analyzer target THIS project's bound collections instead of\n"
        "# deriving them from the folder name:\n"
        f"{cg}\n"
        "\n"
        f"{tail_comment}"
    )


def _registered_kg_class(folder: Path) -> Optional[str]:
    """The project's REGISTERED bound KG primary class, or ``None``.

    MAJOR-R5-4 evidence source for the "no sentinel + no prior manifest"
    arm of :func:`gate`. The identity is consulted ONLY when
    ``identity.registered`` is True — the unregistered path of
    ``resolve_identity`` returns a basename-derived class name, and
    basename derivation is precisely the bug ``project_identity`` exists
    to kill (v0.2.92 W8): counting a basename-derived class could read
    ANOTHER project's collection and misclassify this one from it.

    ``None`` (not resolvable / not registered / no usable class name)
    means the caller cannot positively name this project's KG and must
    keep the pre-R5-4 fresh-install behaviour.
    """
    try:
        from vco_lib.project_identity import resolve_identity

        identity, _snapshot = resolve_identity(folder)
    except Exception:  # noqa: BLE001 — identity must never break the gate
        return None
    if identity is None or not identity.registered:
        return None
    return (identity.kg_primary or "").strip() or None


def _default_kg_counter(class_name: str) -> Optional[int]:
    """The real :param:`count_kg_objects` default — object count for
    ``class_name`` via the Weaviate Aggregate endpoint.

    Delegates to :func:`vco_lib.weaviate_helpers.http_count_objects`, whose
    contract is the one :func:`gate` depends on: ``int`` on success (0 for
    a genuinely empty OR not-yet-created class), ``None`` for unreachable
    / malformed — i.e. UNKNOWN, never zero. Resolved at CALL time from
    this module's namespace so tests can exercise the production
    ``install_project_bundle`` path with a patched counter instead of a
    live Weaviate.
    """
    try:
        from vco_lib.weaviate_helpers import http_count_objects

        return http_count_objects(class_name)
    except Exception:  # noqa: BLE001 — a probe must never break the gate
        return None


def gate(
    folder: Path,
    log: Optional[Callable[..., None]] = None,
    *,
    had_prior_install: bool = False,
    count_kg_objects: Optional[Callable[[str], Optional[int]]] = None,
) -> str:
    """Run the state-keyed chunker-resync detection for ONE project.

    Outcomes (stable strings, logged via ``log`` when given):

    * ``"resync-emitted"``    — stored != current: the existing
      ``project_init._emit_chunker_revision_resync_deferral`` fired (same
      condition_id ``chunker_preset_overhaul_pending`` and remediation as
      the historical gate, so ledger lifecycles and the argparse-swept
      commands are unchanged), and the state is re-stamped.
    * ``"first-observation"`` — nothing stored yet AND this is a fresh
      install: record silently. Nothing is owed — a project's KG built NOW
      is built under the current revision by construction.
    * ``"resync-emitted"`` also covers **no sentinel + a PRIOR BUNDLE
      MANIFEST** (``had_prior_install``; and, failing that, a registered KG
      collection that already holds objects): a
      project that already existed has a KG built under an EARLIER revision,
      so a resync IS owed even though nothing is stored.

      Round-3 BLOCKER-A: this case previously fell into "first-observation"
      because the docstring read "fresh install, OR first run after v0.2.92"
      and treated both as nothing-owed. Since this module is NEW in v0.2.92,
      *every* pre-existing project has no sentinel — so the gate silently
      declared the entire installed base up to date, and the headline repair
      (qwen3 13 500 -> 8 192, which was live silent truncation) reached none
      of them. A missing sentinel on an EXISTING project is evidence the
      project predates the sentinel, not evidence that it is current.
    * ``"resync-emitted"`` ALSO covers **no sentinel + no manifest but a
      NON-EMPTY registered KG class** (MAJOR-R5-4): a project whose
      ``.vco-manifest.json`` was deleted, or that was re-cloned without it,
      is classified fresh by the manifest signal while its Weaviate
      collection still holds objects chunked under the old presets. For
      that population the collection itself is the witness: objects > 0 →
      the KG predates this install → resync emitted exactly as above.
    * ``"unchanged"``         — stored == current.
    * ``"error:<msg>"``       — the sentinel could not be read; no verdict,
      no stamp (the next run re-asks — never guess). Includes
      ``"error:kg-count-unknown"``: the R5-4 evidence probe could not
      reach Weaviate, so "fresh vs pre-existing" is UNRESOLVED — no stamp,
      the next run re-decides (a transient outage must not permanently
      classify the project as fresh).

    Args:
        folder: the project folder.
        log: optional structured logger.
        had_prior_install: the PRIOR manifest existed before this run
            (captured by the caller before anything is written).
        count_kg_objects: keyword-only seam for the R5-4 evidence probe —
            ``class_name -> object count | None``. ``None`` (default)
            resolves to :func:`_default_kg_counter` at CALL time, so tests
            driving the production ``install_project_bundle`` path can
            patch the module-level default instead of needing a live
            Weaviate.

    Soft-fail: never raises, never blocks the install. Called from
    ``install_project_bundle`` on every REAL (non-dry-run) run.
    """
    try:
        # Lazy: project_init is heavy and imports THIS module lazily too.
        from vco_lib.project_init import current_chunker_revision

        current = current_chunker_revision()
    except Exception as e:  # noqa: BLE001 — no sentinel, no verdict
        outcome = f"error:{type(e).__name__}: {e}"
    else:
        previous = read_last_revision(folder)
        if previous is None and had_prior_install:
            # Round-3 BLOCKER-A. A project that ALREADY HAD a bundle manifest
            # was installed by an earlier orchestrator, so its KG was built
            # under an earlier chunker revision and the resync IS owed.
            # Reading a missing sentinel as "current" silently excluded the
            # entire installed base from the repair — and since this module is
            # new in v0.2.92, that was EVERY existing project.
            #
            # The signal is the PRIOR MANIFEST, not `update_mode`: a brand-new
            # project can be created by an update run, and calling that a
            # resync would nag every fresh install forever.
            try:
                from vco_lib.project_init import (
                    _emit_chunker_revision_resync_deferral,
                )

                _emit_chunker_revision_resync_deferral(
                    folder, "pre-v0.2.92 (no sentinel)", current
                )
                outcome = "resync-emitted"
            except Exception as e:  # noqa: BLE001 — deferral write soft-fails
                outcome = f"error:{type(e).__name__}: {e}"
        elif previous is None:
            # No sentinel AND no prior manifest. Fresh install is the common
            # case — but not the only one (MAJOR-R5-4): a project whose
            # manifest was deleted, or that was re-cloned without it, lands
            # here classified "fresh" while its Weaviate collection still
            # holds objects chunked under the old presets. One more piece of
            # evidence before concluding "fresh": does this project's
            # REGISTERED bound KG class already hold objects? A re-clone's
            # collection is non-empty; a genuinely fresh add's is empty or
            # not yet created (Aggregate over a missing class is 0, not
            # unknown). The probe runs ONLY on this path, so a fresh add
            # pays one cheap count and stays silent.
            kg_class = _registered_kg_class(folder)
            if kg_class is None:
                # Not resolvable / not registered: we cannot POSITIVELY
                # name this project's KG (a basename guess is the W8 bug),
                # so keep the pre-R5-4 fresh-install behaviour.
                outcome = "first-observation"
            else:
                counter = (
                    count_kg_objects
                    if count_kg_objects is not None
                    else _default_kg_counter
                )
                try:
                    count = counter(kg_class)
                except Exception:  # noqa: BLE001 — probe must never raise
                    count = None
                if count is None:
                    # UNKNOWN (Weaviate unreachable / malformed response):
                    # no verdict and NO STAMP — stamping "current" on an
                    # unknown would permanently misclassify the project
                    # from one transient outage. The next run re-decides.
                    outcome = "error:kg-count-unknown"
                elif count > 0:
                    # This project's KG predates this install: emit the
                    # resync exactly as the had_prior_install arm does.
                    # The emitted ledger entry then arms the printed
                    # remedy (kg-sync --all) for this population too.
                    try:
                        from vco_lib.project_init import (
                            _emit_chunker_revision_resync_deferral,
                        )

                        _emit_chunker_revision_resync_deferral(
                            folder,
                            "pre-v0.2.92 (no sentinel; KG collection "
                            "non-empty)",
                            current,
                        )
                        outcome = "resync-emitted"
                    except Exception as e:  # noqa: BLE001 — deferral soft-fails
                        outcome = f"error:{type(e).__name__}: {e}"
                else:
                    # Genuinely fresh (0 objects): built NOW under the
                    # current revision, nothing owed.
                    outcome = "first-observation"
        elif previous != current:
            try:
                from vco_lib.project_init import (
                    _emit_chunker_revision_resync_deferral,
                )

                _emit_chunker_revision_resync_deferral(
                    folder, previous, current
                )
                outcome = "resync-emitted"
            except Exception as e:  # noqa: BLE001 — deferral write soft-fails
                outcome = f"error:{type(e).__name__}: {e}"
        else:
            outcome = "unchanged"
        # Stamp only when there is something new to record. `write_last_revision`
        # rewrites `updated_at` on every call, so stamping an "unchanged" outcome
        # makes two consecutive bundle runs produce DIFFERENT bytes — breaking
        # install idempotency whenever they straddle a wall-clock second (a
        # load-dependent flake over a deterministic defect). Nothing reads
        # `updated_at`; the only consumer is `read_last_revision`, which reads
        # `revision`. So the field means "when the revision last CHANGED", and
        # skipping the no-op write is both correct and stable. The allowlist
        # (not `!= "unchanged"`) also keeps the documented promise that every
        # `error:` outcome — including `error:kg-count-unknown` — leaves the
        # sentinel unstamped so the next run re-asks, never guesses.
        if outcome in ("resync-emitted", "first-observation"):
            write_last_revision(folder, current)
    if log is not None:
        try:
            log("4.bundle.chunker_revision", "end",
                f"chunker revision gate: {outcome}")
        except Exception:  # noqa: BLE001
            pass
    return outcome
