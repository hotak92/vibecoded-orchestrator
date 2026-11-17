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


# ---------------------------------------------------------------------------
# The RESYNC half-stamps — the clear mechanism the deferral always promised
# ---------------------------------------------------------------------------
#
# Both emitters of ``chunker_preset_overhaul_pending`` tell the user, in the
# shipped entry text: *"Once you run the re-sync commands below, this deferral
# self-resolves on the next bundle update."* Until v0.2.95 nothing implemented
# that. The registry declared ``clear_probe = "paired-resolution"`` ("the
# component that emitted this entry clears it when its owed work completes")
# and named "the Rust chunker-revision flow" as the site — which has no
# resolve call, in either language. Worse, :func:`gate` re-stamps
# :data:`STATE_REL` at EMIT time, so every later run reads stored == live and
# the only comparison that could have re-derived anything is permanently
# satisfied. The row was immortal by construction, and it is load-bearing in a
# second place: ``templates/scripts/sync_knowledge_graph.py`` arms a per-node
# chunk-plan comparison *while the ledger carries the cid* and documents that
# cost as transitional. A cid that never clears made it forever.
#
# The fix keeps the promise instead of retiring it (the standing rule: a false
# promise is MADE TRUE by default; deleting one is the user's call). The
# remedy :func:`resync_commands` prints has exactly two halves, so the
# evidence does too: each half stamps the revision it ran under, and the
# registry probe ``chunker_resync_still_owed`` reports the condition over only
# when BOTH name the CURRENT revision.
#
# Why a stamp rather than re-measuring the stores: re-deriving "are any stored
# chunk boundaries stale?" means fetching every KG row and re-planning it —
# the exact work the comparison does, on a probe that runs on every ledger
# render. The stamp is written at the ONE point that proves the half ran (a
# fully successful tree sync; a completed ``--force-recreate`` walk), which is
# the same narrowness the sibling paired clears in both scripts already use.
#
# v0.2.107 — two holes in that design, both closed here. (1) The KG sync kept
# its per-node comparison (and the split-rows re-lay) armed for as long as the
# ENTRY lived, i.e. until the code-graph half was recorded too, so every
# `--all` in between re-embedded the same nodes; it now asks
# :func:`kg_half_recorded`. (2) The analyzer recorded the code-graph half in
# the ledger root (`$VCT_ORCHESTRATOR_ROOT`) instead of the project it walked,
# so only the orchestrator root could ever clear; it is recorded in the walked
# project now, and :func:`completed_walk_evidence` / :func:`heal_stuck_resync`
# retire the entry of a project whose walk had already completed.
#
# A LATER revision crossing re-arms everything for free: :func:`gate` emits
# again, and stamps recorded under the old revision no longer match, so the
# probe says "still owed" without anyone having to clear them.

#: Per-project record of which halves of the re-sync remedy have run, relative
#: to the project folder. Sibling of :data:`STATE_REL` and deliberately a
#: SEPARATE file: that one answers "which revision did this project last
#: observe?" (written by the gate), this one "which halves of the remedy have
#: run, and under which revision?" (written by the remedy scripts).
RESYNC_STATE_REL = Path(".claude") / "state" / "chunker-resync.json"

#: The halves of :func:`resync_commands`, in the order it prints them. Both
#: must name the current revision before the deferral describes work that is
#: over — clearing on one would retire an entry whose other store is still
#: chunked under the old boundaries.
RESYNC_HALVES = ("kg", "codegraph")


def resync_state_path(folder: Path) -> Path:
    """Absolute path of the half-stamp file under ``folder``."""
    return Path(folder) / RESYNC_STATE_REL


def current_revision() -> Optional[str]:
    """The live ``_CHUNKER_REVISION``, or ``None`` when it cannot be read.

    ONE soft-failing accessor for the readers that must not raise (the stamp
    writers and the clear probe). :func:`gate` keeps its own call because it
    turns the failure into an ``error:`` outcome; here "could not look" is
    simply unknown, which every caller maps to "change nothing".
    """
    try:
        from vco_lib.project_init import current_chunker_revision

        revision = current_chunker_revision()
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None
    return revision if isinstance(revision, str) and revision.strip() else None


def read_resync_stamps(folder: Path) -> Optional[dict]:
    """The recorded half → revision map for ``folder``.

    Returns:
        ``{}``   — no stamp file yet: nothing has been recorded, which is a
                   KNOWN state (no half has run), not an unreadable one.
        ``dict`` — the recorded halves (only ``str`` → non-empty ``str``
                   pairs survive; anything else is dropped as unrecorded).
        ``None`` — the file exists and could not be read or parsed. Unknown,
                   never "nothing recorded": the difference decides whether a
                   user-visible entry may be deleted.
    """
    path = resync_state_path(folder)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    halves = data.get("halves")
    if not isinstance(halves, dict):
        return None
    return {
        k: v.strip()
        for k, v in halves.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


def record_resync_half(
    folder: Path, half: str, revision: Optional[str] = None,
) -> bool:
    """Record that ``half`` of the re-sync remedy completed under ``revision``.

    Merges into whatever is already recorded — the two halves are run
    independently, in either order, often days apart, so a write that replaced
    the file would make each half retire the other.

    ``revision`` defaults to :func:`current_revision`; ``None`` (unreadable
    chunker) records NOTHING and returns False rather than stamping a guess,
    because a wrong revision here either retires a live entry or immortalises
    a dead one.

    Never raises. Returns whether the stamp landed, so a caller can log it —
    never gate on it: the remedy's real work already succeeded, and a failed
    stamp only means the entry survives until the half is run again.
    """
    if half not in RESYNC_HALVES:
        return False
    rev = (revision or "").strip() or current_revision()
    if not rev:
        return False
    existing = read_resync_stamps(folder)
    halves = dict(existing) if isinstance(existing, dict) else {}
    halves[half] = rev
    try:
        target = resync_state_path(folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"halves": halves, "updated_at": _now_iso()}) + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
        return True
    except OSError:
        return False


def resync_still_owed(folder: Path) -> Optional[bool]:
    """Is the chunker re-sync ``folder``'s ledger entry describes still owed?

    The registry clear probe for ``chunker_preset_overhaul_pending``
    (``probe:py:chunker_resync_still_owed``).

    Returns:
        True  — at least one half has not run under the current revision
                (including "no stamp file at all", the state every machine
                that has not acted is in).
        False — provably over: every half in :data:`RESYNC_HALVES` is
                recorded at the CURRENT revision — or only the code-graph
                half is unrecorded and :func:`completed_walk_evidence` shows
                the project's forced walk completed under it.
        None  — could not look (the chunker revision is unreadable, or the
                stamp file exists and is corrupt). Positive evidence only: an
                unreadable stamp never retires a user-visible record of owed
                work.
    """
    current = current_revision()
    if not current:
        return None
    stamps = read_resync_stamps(folder)
    if stamps is None:
        return None
    missing = [half for half in RESYNC_HALVES if stamps.get(half) != current]
    if missing == ["codegraph"] and completed_walk_evidence(folder):
        # v0.2.107: the walk ran, its half was recorded in another tree (see
        # `completed_walk_evidence`). Read-only here — probes write nothing;
        # `heal_stuck_resync` is the writer.
        return False
    return bool(missing)


def kg_half_recorded(folder: Path) -> Optional[bool]:
    """Is the KG half of the re-sync recorded at the CURRENT revision?

    The ONE reading of "the KG half has run" — :func:`kg_rechunk_owed` (the
    update's trigger) and ``sync_knowledge_graph.py``'s per-node gate
    (``_chunker_resync_pending``) both ask it, so "start a pass" and "re-lay
    this node" cannot disagree. They did until v0.2.107: the gate read only
    the ledger entry, which stays until the CODE-GRAPH half is recorded too,
    so every ``--all`` in between re-embedded the same nodes again.

    Returns True / False on a readable state; ``None`` when the revision or
    the stamp file cannot be read (unknown — each caller keeps its own
    conservative arm).
    """
    current = current_revision()
    if not current:
        return None
    stamps = read_resync_stamps(folder)
    if stamps is None:
        return None
    return stamps.get("kg") == current


def _parse_utc(value: object) -> Optional[datetime]:
    """A ``%Y-%m-%dT%H:%M:%SZ`` stamp as an aware datetime, else ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def completed_walk_evidence(folder: Path) -> bool:
    """Positive evidence that ``folder``'s whole-repo forced code-graph walk
    COMPLETED under the current chunker revision. Read-only, never raises.

    Why it exists (v0.2.107): until this release the analyzer recorded the
    code-graph half in ``$VCT_ORCHESTRATOR_ROOT`` instead of the project it
    walked, so on every project except the orchestrator root the walk the
    update runs finished and the half was never recorded where the probe
    reads — the entry could not clear. The walk itself left a record in the
    RIGHT place: ``codegraph_extractor_generation.stamp_after_walk`` writes
    the project's own generation stamp, and only a clean whole-repo forced
    walk writes it with a ``force-rewalk completed…`` note.

    True only when ALL of these are read:

    * the generation stamp names the CURRENT extractor generation, was
      written by that module, and its note starts ``force-rewalk completed``
      (a stamp set without a walk — ``version_at_or_past_bump``, ``no_graph``
      — carries another note and proves nothing);
    * the project's chunker sentinel (:data:`STATE_REL`, written by
      :func:`gate` when it emitted the entry) names the CURRENT revision;
    * the walk's ``written_at`` is not earlier than the sentinel's
      ``updated_at`` — the walk finished after this project crossed to the
      current revision, so it planned every entity with the current chunker.

    Anything missing, unreadable or out of order is ``False``: the half is
    then recorded only by a new walk, exactly as before.
    """
    try:
        current = current_revision()
        if not current:
            return False
        from vco_lib import codegraph_extractor_generation as ceg

        walk = ceg.read_stamp_payload(Path(folder))
        if not isinstance(walk, dict):
            return False
        note = walk.get("note")
        if (
            walk.get("generation") != ceg.CURRENT_EXTRACTOR_GENERATION
            or walk.get("written_by") != "vco_lib.codegraph_extractor_generation"
            or not isinstance(note, str)
            or not note.startswith("force-rewalk completed")
        ):
            return False
        sentinel = json.loads(state_path(Path(folder)).read_text(encoding="utf-8"))
        if not isinstance(sentinel, dict) or sentinel.get("revision") != current:
            return False
        walked_at = _parse_utc(walk.get("written_at"))
        crossed_at = _parse_utc(sentinel.get("updated_at"))
        if walked_at is None or crossed_at is None:
            return False
        return walked_at >= crossed_at
    except Exception:  # noqa: BLE001 — could not look is not evidence
        return False


def heal_stuck_resync(folder: Path) -> bool:
    """Retire ``folder``'s chunker entry when its remedy has provably run.

    The session-start counterpart of the registry probe (which clears the same
    entry on every update and bundle update): a project whose forced walk
    completed while its code-graph half went to another tree (see
    :func:`completed_walk_evidence`) gets the half recorded HERE, in its own
    ``chunker-resync.json``, and — once both halves name the current revision
    — its ledger entry resolved, without a new walk.

    Leaves everything alone unless the ledger carries :data:`RESYNC_CID` and
    :func:`resync_still_owed` answers a positive ``False``. Returns whether
    the entry was resolved. Never raises.
    """
    try:
        from vco_lib.deferral_report import DeferralReport

        root = Path(folder)
        if not DeferralReport.read(root).has_condition(RESYNC_CID):
            return False
        current = current_revision()
        stamps = read_resync_stamps(root)
        if not current or stamps is None:
            return False
        if stamps.get("codegraph") != current and completed_walk_evidence(root):
            record_resync_half(root, "codegraph", current)
        if resync_still_owed(root) is not False:
            return False
        from vco_lib.deferral_emit import resolve_conditions

        if resolve_conditions(root, (RESYNC_CID,)) <= 0:
            return False
        try:
            from vco_lib.deferral_probes import record_probe_resolution

            record_probe_resolution(root, RESYNC_CID, "chunker_resync_still_owed")
        except Exception:  # noqa: BLE001 — the audit row is best-effort
            pass
        return True
    except Exception:  # noqa: BLE001 — a heal must never break a session start
        return False


#: The ledger condition the revision gate emits. One name for the readers
#: in this module; the emitter (``project_init``) and the sync script carry
#: the same literal, pinned by the registry (``deferral_conditions.toml``).
RESYNC_CID = "chunker_preset_overhaul_pending"


def kg_rechunk_owed(folder: Path) -> Optional[bool]:
    """Does ``folder`` still owe the KG half of a chunker re-sync?

    v0.2.106 (owner ruling "fix + re-embed": every install re-chunks its KG
    ONCE after updating). The re-chunk is an ordinary hash-gated
    ``kg-sync --all`` run while the ledger carries :data:`RESYNC_CID` — the
    entry arms the per-node plan comparison, so only nodes whose stored plan
    differs from the current chunker re-embed, and a clean run stamps the KG
    half (:func:`record_resync_half`). What was missing is a TRIGGER: the
    content-hash drift scan cannot see a boundary change (the text did not
    change), so neither update path ever started that run on its own. This
    is the question both of them now ask — the launcher's bundle update
    through ``kg-sync --check-drift`` (``rechunk_owed`` on its sentinel) and
    ``install.py``'s root seed.

    Returns:
        True  — the ledger carries the entry AND the KG half is not recorded
                at the current revision: a whole-tree pass is owed.
        False — no entry (nothing crossed), or the KG half already ran under
                the current revision. A second update therefore does nothing.
        None  — could not look (revision, ledger or stamp file unreadable).
                Callers treat it as "not owed": an unreadable state must not
                start a re-embed on every update.
    """
    current = current_revision()
    if not current:
        return None
    try:
        from vco_lib.deferral_report import DeferralReport

        pending = DeferralReport.read(Path(folder)).has_condition(RESYNC_CID)
    except Exception:  # noqa: BLE001 — could not look is not a verdict
        return None
    if not pending:
        return False
    recorded = kg_half_recorded(folder)
    return None if recorded is None else not recorded


def resync_commands(folder: Path, tail_comment: str) -> str:
    """The ONE re-chunk remediation the chunker deferral emits.

    The emitter is ``project_init._emit_chunker_revision_resync_deferral``
    (called from the Python bundle engine and, via ``python -c``, from the
    launcher's Rust revision hook), and the ``condition_id`` it writes is
    shared with the resolver — so the commands MUST stay this one text. One
    builder, every caller. It lives here rather than in ``project_init``
    because this module owns the chunker-resync lifecycle and
    ``project_init`` is ratchet-capped (its call sites stay thin). (The
    v0.2.46 SEMVER-boundary sibling emitter was retired in v0.2.103, owner
    decision 2026-10-15 — superseded by the revision-keyed paths.)

    Shape (v0.2.92, BLOCKER-2 + the R42 portability sweep; v0.2.106):

    * ``--force-rewalk``, NOT ``--force-recreate`` (v0.2.106). The re-chunk
      the code-graph half needs is a walk that re-plans every entity with the
      current chunker — ``--force-rewalk`` bypasses the per-file gate, and the
      per-entity / per-chunk content hashes then re-embed exactly the entities
      whose chunks changed. ``--force-recreate`` bought the same re-chunk by
      DROPPING the five ``<prefix>_Code*`` classes first: the graph was empty
      until the rebuild finished, and gone if it failed. A completed
      whole-repo force re-walk stamps the half (``codegraph_deferrals.
      record_successful_walk``), so this command retires the entry too — and
      the extractor-generation rung a chunker bump carries runs that walk in
      the background on update, so usually nobody has to.
    * ``--from-resolver`` on the analyzer. Without an identity flag the
      analyzer's last resolution rung is the folder BASENAME (``.claude/env``
      is shell-sourced for hooks, not for the terminal the user pastes this
      into) — which walks the wrong family for any moved/renamed project.
      This is the flag the per-edit hooks and the launcher already use.
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
        remedy_shell.quote(str(folder)), "--from-resolver", "--force-rewalk",
    )
    return (
        "# Re-chunk this project's KG (content-hash gated: only entries whose\n"
        "# chunks changed re-embed; nothing is deleted before its new vectors\n"
        "# are written):\n"
        f"{kg}\n"
        "\n"
        "# Re-chunk this project's code graph: re-walk every file (nothing is\n"
        "# dropped; entities whose chunks changed re-embed, the rest are\n"
        "# hash-skipped). --from-resolver makes the analyzer target THIS\n"
        "# project's bound collections instead of deriving them from the\n"
        "# folder name:\n"
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

    Delegates to :func:`vco_lib.weaviate_helpers.http_count_objects` with
    ``absent_is_zero=True``, the contract :func:`gate` depends on: ``int`` on
    success (0 for a genuinely empty class, or one the schema endpoint
    positively reports as not yet created), ``None`` for unreachable / an
    error reply / malformed — i.e. UNKNOWN, never zero. Resolved at CALL time from
    this module's namespace so tests can exercise the production
    ``install_project_bundle`` path with a patched counter instead of a
    live Weaviate.
    """
    try:
        from vco_lib.weaviate_helpers import http_count_objects

        return http_count_objects(class_name, absent_is_zero=True)
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
            # not yet created (the counter reads a class the schema endpoint
            # reports absent as 0, not unknown). The probe runs ONLY on this
            # path, so a fresh add pays one cheap count and stays silent.
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
