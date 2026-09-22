# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Exposure-conditional ONE-TIME code-graph re-embed (v0.2.96, register M-2).

THE PROBLEM (register issue 8, 2026-09-20).  A pre-v0.2.92 ``code_embed``
image SILENTLY TRUNCATES over-window input at HTTP 200.  Rows embedded
through such an image carry CORRECT content hashes — truncation corrupts
VECTORS, not text — so every content-hash / embed-revision gate skips them
forever and nothing heals them.  WP-3
(:func:`vco_lib.codegraph_resync.code_embed_image_verdict` + the gates in
``spawn_background_resync`` / ``run_resync_and_verify``) stopped NEW resyncs
from embedding through a stale image; this module heals the HISTORICAL
damage with an exposure-conditional, one-time bump.

COHORT SCOPE — why the evidence is narrowed to the truncating cohort.
``served_state`` (vco_lib.code_embed_image) distinguishes two STALE shapes:

* ``/health`` WITHOUT the ``source_sha`` KEY → the image predates v0.2.92 →
  it TRUNCATES silently → this is the only population that can carry
  corrupted-but-hash-current rows (``served_sha is None`` on the verdict);
* ``source_sha`` present but a different digest → the image HAS the v0.2.92
  ``server.py``, which REFUSES over-window input at HTTP 400 → its failed
  embeds land as vectorless (``embed_revision=0``) rows that the EXISTING
  revision gate already re-embeds.  No silent corruption, no bump owed.

Firing on the second cohort would cost healthy machines a full redundant
re-embed, so exposure evidence is restricted to the first.

STATE MACHINE (one-shot PER PROJECT; marker under ``vct_root_dir()``):

The marker file holds THREE blocks (v0.2.96 follow-ups, register M-2 J-2 and
the ship-gate's MAJOR-3)::

    {"observed": {queued_at, reason, evidence},   # machine-level, first-wins
     "demoted":  {"<project_name>": "<ISO ts>"},  # per-project demote log
     "healed":   {"<project_name>": "<ISO ts>"}}  # per-project discharge log

``observed`` is machine-level because the truncating image WAS machine-level
—one fact for every project.  The OWED state is per-project: a project owes
the heal while ``observed`` exists and the project has no ``healed`` entry.
(The original marker was machine-level one-shot — the FIRST project's heal
cleared it and every other exposed project kept its truncated rows with only
``--force-recreate`` as the escape.  Each project's rows were embedded
through the same truncating image, so each owes exactly one heal.)

``demoted`` bounds the EXPENSIVE half.  The discharge (``healed``) requires
positive convergence, which a project can legitimately never reach (the
resync driver has a whole NO-PROGRESS branch for it).  While the demote was
driven by the owed state alone, such a project re-demoted its entire
current-revision set on EVERY owed resync and re-embedded the whole graph
each time — unbounded, silent, and the exact opposite of the "ONE-TIME"
this module promises.  A project is therefore demoted at most once per
exposure era; a residual that never converges surfaces through the existing
unconverged deferral, not through another full re-embed.  Only a FULLY
successful demote is recorded: a pass that left rows un-demoted has not done
the work, and the next resync retries it (the demote skips rows already at
the sentinel, so the retry only touches the remainder).

1. **OBSERVE** — :func:`observe_stale_image`, called from
   ``code_embed_image.plan_rebuild`` (the installer's compose-up decision),
   persists a truncating-cohort observation BEFORE the rebuild it plans can
   erase the live evidence (an owned machine's image is rebuilt by the same
   update whose maintenance step runs detection afterwards).
2. **DETECT** — :func:`detect_and_queue`, called thinly from install.py's
   codegraph-maintenance step, for the project whose maintenance step is
   running.  Exposure = stale evidence (live probe OR persisted observation
   OR a ``code_embed_image_stale`` ledger entry whose text carries the
   pre-v0.2.92 signature) AND completion evidence (positively: rows AT the
   current embed revision exist for the project — see
   ``codegraph_resync.has_rows_at_current_revision`` for why that is the
   strongest available signal).  Both hold → write the machine-level
   ``observed`` block (first-wins; every not-yet-healed project derives its
   owed state from it, including projects whose own detection never ran —
   their spawn owed-gate consults the same marker).  Either missing →
   nothing is written and the machine behaves byte-for-byte as before this
   module existed.  Idempotent PER PROJECT: a project already owed is never
   re-queued, and a project with a ``healed`` entry never re-fires.
   :func:`detect_from_history` is the SECOND entry point, called by the
   resync driver for its own project: update-time detection keys completion
   evidence on ONE project's rows, so a root with none recorded nothing and
   left every project unowed (the M-2 residual gap, closed 2026-09-22).  It
   uses the machine-level history only — the caller has just proven the
   image current, so a live probe could say nothing about rows already
   stored.
3. **HEAL** — the resync driver (``run_resync_and_verify``), AFTER its WP-3
   gate and only under a POSITIVELY ``current`` image verdict, demotes the
   project's current-revision rows to the vectorless sentinel so the
   EXISTING revision gate forces exactly those rows to re-embed once through
   the fixed service (``codegraph_resync.demote_current_revision_rows``),
   records ``demoted[project]`` when that demote fully succeeded, and — on
   positive convergence, in that pass or a later one — records
   ``healed[project]``.  Other projects' owed state is untouched; each heals
   on its own next resync, exactly once.

LEGACY (v1) MARKER — backward-compatible reading.  The v1 file was
machine-level: its PRESENCE was the owed flag and the heal DELETED it.
Migration rules:

* v1 file PRESENT (top-level ``queued_at``/``reason``/``evidence``, no
  ``observed``/``healed`` keys) → read as ``{"observed": <v1 payload>,
  "healed": {}}``.  A v1 machine that owed the heal has not verifiably
  healed ANY project (the v1 clear deleted all evidence of partial
  discharge), so conservatively every project owes and each heals once.
* v1 file ABSENT (never queued, or already cleared machine-wide) → no
  marker, and NO suppression is fabricated from the absence: detection
  stays evidence-driven, so a project that still owes re-records
  ``observed`` from a live probe / observation / surviving ledger entry on
  the next update.  A legacy "already healed" machine therefore never
  suppresses per-project detection.

Non-exposed machines pay nothing: no marker, no demote, the content-hash
gate stays authoritative, zero extra embeds.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

#: One-shot owed-marker, under ``vct_root_dir()`` (~/.vct).  Holds the
#: machine-level ``observed`` evidence block (the truncating service was
#: machine-level: it corrupted every project's embeds alike) AND the
#: per-project ``healed`` discharge map (the heal — and therefore the owed
#: state — is per project, because each project's rows re-embed exactly
#: once; see the module docstring's state machine and legacy-migration
#: rules).
MARKER_REL = Path("state") / "code_embed_exposure_bump.json"

#: Persisted truncating-cohort observation (written by ``plan_rebuild`` when
#: it sees the pre-v0.2.92 shape live; consumed when detection queues the
#: marker).  Distinct from the marker: this is EVIDENCE, that is OWED WORK.
OBSERVATION_REL = Path("state") / "code_embed_truncating_image_observed.json"

#: The ``served_state`` summary signature that positively identifies the
#: truncating cohort.  MUST MATCH ``vco_lib.code_embed_image.served_state``'s
#: no-``source_sha``-key branch (the doctor's ``code_embed_image_stale``
#: ledger entry carries that summary verbatim as its ``detected`` text, which
#: is what :func:`ledger_shows_truncating_image` scans for).
#:
#: It is a DISCRIMINATOR, not a phrase to keep in step with the prose around
#: it.  The doctor's entry ``title`` and ``why_deferred`` are SHARED by both
#: stale cohorts, and the why_deferred already talks about truncation ("A
#: pre-v0.2.92 image TRUNCATES over-window code…").  Rewording either of them
#: INTO this string would make a digest-MISMATCH entry — an image that
#: carries v0.2.92's server and therefore REFUSES over-window input at HTTP
#: 400 — read as truncating-cohort evidence, and a healthy machine would pay
#: a full redundant re-embed.  Both directions are pinned in
#: ``tests/test_v0296_exposure_bump.py`` THROUGH the real doctor builder.
TRUNCATION_SIGNATURE = "predates v0.2.92"

#: The doctor's condition id for a stale image.  MUST MATCH
#: ``vco_lib.doctor.CID_CODE_EMBED_IMAGE_STALE`` (imported lazily below where
#: practical; the literal here keeps this module import-light).
CID_IMAGE_STALE = "code_embed_image_stale"

#: detect_and_queue status tokens (the install shim logs them verbatim).
STATUS_QUEUED = "queued"
STATUS_ALREADY_QUEUED = "already_queued"
STATUS_NOT_EXPOSED = "not_exposed"
STATUS_UNDETERMINABLE = "undeterminable"


def _marker_path() -> Path:
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / MARKER_REL


def _marker_lock_path() -> Path:
    """Sidecar lock token for the marker's read-merge-write (see
    :func:`_marker_lock`).  Contents are irrelevant; it is safe to leave on
    disk between runs (the state dir is user-owned and git-ignored)."""
    return _marker_path().with_name(_marker_path().name + ".lock")


def _observation_path() -> Path:
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / OBSERVATION_REL


def _write_json(path: Path, payload: dict) -> bool:
    try:
        # v0.2.96: the ONE atomic-write home (governance test
        # tests/test_v0292_atomic_one_home.py) — never a hand-rolled
        # tmp+rename beside it.
        from vco_lib.atomic import atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload)
        return True
    except Exception as exc:  # noqa: BLE001 — state writes never crash callers
        logger.warning("code_embed_exposure: cannot write %s: %s", path, exc)
        return False


def _read_json(path: Path) -> Optional[dict]:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 — unreadable state is "no state"
        logger.warning("code_embed_exposure: cannot read %s: %s", path, exc)
        return None


def _is_truncating_state(state: Any) -> bool:
    """True iff an ImageState is the pre-v0.2.92 truncating cohort.

    STALE with ``served_sha is None`` is exactly ``served_state``'s
    "no ``source_sha`` KEY" branch — a digest MISMATCH (served_sha set) is a
    post-v0.2.92 image that refuses loudly and is therefore NOT exposure.
    """
    return bool(
        getattr(state, "is_stale", False)
        and getattr(state, "served_sha", None) is None
    )


# ── 1. OBSERVE ───────────────────────────────────────────────────────────────


def observe_stale_image(state: Any) -> None:
    """Persist a truncating-cohort observation (soft-fail, first-wins).

    Called from ``code_embed_image.plan_rebuild`` whenever it computes a
    STALE verdict — the moment on an OWNED machine where the live evidence
    is about to be erased by the rebuild plan_rebuild itself returns.  Only
    the truncating cohort is persisted (see :func:`_is_truncating_state`);
    an existing observation is kept (earliest evidence wins, and a re-write
    would gain nothing — the fact is binary).
    """
    try:
        if not _is_truncating_state(state):
            return
        path = _observation_path()
        if path.is_file():
            return
        _write_json(path, {
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "verdict": getattr(state, "verdict", "stale"),
            "expected_sha": getattr(state, "expected_sha", None),
            "served_sha": None,
            "summary": getattr(state, "summary", ""),
        })
    except Exception as exc:  # noqa: BLE001 — observation never gates anything
        logger.debug("code_embed_exposure: observation write skipped: %s", exc)


def read_stale_observation() -> Optional[dict]:
    """The persisted truncating observation, or ``None``."""
    return _read_json(_observation_path())


def consume_stale_observation() -> None:
    """Delete the observation (queued into the marker — evidence absorbed).

    Load-bearing for one-shot semantics: an observation that outlived the
    heal would re-queue the marker on every subsequent update.  Soft-fail:
    a recorded ``observed`` block is the stronger state; a leftover
    observation can at worst produce one redundant (idempotent) queue
    attempt, which :func:`queue_exposure_bump` declines while an observation
    is recorded — and after the heal the NEXT observation would have to be
    a genuinely new stale era.  Still deleted here so that guarantee does
    not rest on timing.  Machine-level, like the ``observed`` block it fed:
    consuming it after the first project's queue is correct because every
    other project's owed state derives from ``observed``, not from this
    file.
    """
    try:
        path = _observation_path()
        if path.is_file():
            path.unlink()
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_embed_exposure: cannot consume observation: %s", exc)


# ── 2. marker primitives (per-project owed/healed bookkeeping) ──────────────


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _project_map(raw: Any) -> dict:
    """One per-project block (``healed`` / ``demoted``), normalized.

    Non-dicts read as empty (never fabricate per-project state), and
    non-string keys are dropped — the callers key on the code-graph project
    NAME, so anything else could only match by accident.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(k, str)}


def _read_marker() -> Optional[dict]:
    """The marker, normalized to
    ``{"observed": dict|None, "demoted": dict, "healed": dict}``.

    Backward-compatible reading of the v1 machine-level shape (see the
    module docstring's LEGACY MARKER rules): a v1 file — top-level
    ``queued_at``/``reason``/``evidence``, no ``observed``/``healed`` keys —
    migrates ON READ to ``observed=<the v1 payload>, healed={}``.  The
    migration is conservative in the owed direction: a v1 marker that was
    still queued has not verifiably discharged ANY project (the v1 heal
    deleted the whole file), so ``healed`` starts empty and every project
    owes its one heal.  A v1 marker that was already cleared is simply
    ABSENT — no suppression is fabricated from the absence.  Unknown or
    corrupt shapes read as "no marker" (never fabricate owed state); the
    migration is written back on the next marker write (lazy — reads never
    write).
    """
    data = _read_json(_marker_path())
    if data is None:
        return None
    if "observed" in data or "healed" in data or "demoted" in data:
        observed = data.get("observed")
        return {
            "observed": observed if isinstance(observed, dict) else None,
            "demoted": _project_map(data.get("demoted")),
            "healed": _project_map(data.get("healed")),
        }
    if "queued_at" in data or "reason" in data or "evidence" in data:
        # v1 machine-level marker: presence WAS the owed flag.
        logger.info(
            "code_embed_exposure: migrating legacy machine-level marker to "
            "the per-project shape (healed map starts empty — every project "
            "owes its one heal)"
        )
        return {"observed": dict(data), "demoted": {}, "healed": {}}
    return None


@contextmanager
def _marker_lock() -> Iterator[None]:
    """Serialize the marker's read-merge-write cycle across processes.

    ``atomic_write_json`` makes each WRITE atomic but not the READ-merge-WRITE
    around it: two detached resync drivers healing DIFFERENT projects (the
    realistic shape on a multi-project install — "Update all bundles", or a
    post-rebuild deferral-retry wave) both read ``healed={}``, both write, and
    the second write erases the first project's discharge — so that project
    re-heals and pays a second full re-embed.  ``vco_lib.atomic.exclusive_file_lock``
    is the ONE home for this primitive (precedent: ``deferral_emit.locked_report``,
    same class of machine-shared-file RMW); the lock token is a sidecar beside
    the marker, under the git-ignored state dir.

    Never nest it — on POSIX the second ``flock(LOCK_EX)`` from the same
    process would deadlock it against itself (the same caveat
    ``deferral_emit`` documents).  Keep the locked blocks flat and short.
    """
    from vco_lib.atomic import exclusive_file_lock

    with exclusive_file_lock(_marker_lock_path()):
        yield


def _record_project_stamp(block: str, project: str) -> Optional[bool]:
    """Stamp ``marker[<block>][project] = now`` under the marker lock.

    ONE home for the two per-project bookkeeping blocks — ``healed`` (the
    discharge) and ``demoted`` (the one-time expensive half) — which are the
    same read-merge-write with a different key.

    Tri-state: ``True`` written, ``False`` refused (marker present but
    unreadable — fabricating an entry over an observation we cannot verify is
    exactly the under-heal this shape exists to prevent), ``None`` when there
    is NO marker file at all (nothing is owed; the caller decides what that
    means for it — no file is ever created here).
    """
    path = _marker_path()
    with _marker_lock():
        if not path.is_file():
            return None
        marker = _read_marker()
        if marker is None:
            logger.warning(
                "code_embed_exposure: marker unreadable — %s stamp refused "
                "(the next resync retries)", block,
            )
            return False
        stamped = dict(marker.get(block) or {})
        stamped[str(project)] = _now()
        payload = {
            "observed": marker.get("observed"),
            "demoted": dict(marker.get("demoted") or {}),
            "healed": dict(marker.get("healed") or {}),
        }
        payload[block] = stamped
        return _write_json(path, payload)


def queue_exposure_bump(evidence: dict) -> bool:
    """Write the machine-level ``observed`` block.  Idempotent, first-wins:
    an existing observation is NEVER overwritten (its ``queued_at``/evidence
    stay the original ones) and ``False`` is returned, so re-running
    detection cannot re-fire.  The ``healed`` and ``demoted`` maps already in
    the marker are preserved across the write — one project's discharge never
    disturbs another's owed state.

    The whole read-merge-write runs under :func:`_marker_lock`: the marker is
    machine-shared, and a concurrent per-project discharge must not be
    overwritten from a snapshot taken before it."""
    path = _marker_path()
    with _marker_lock():
        existing = _read_marker()
        if existing is not None and existing.get("observed") is not None:
            return False
        payload = {
            "observed": {
                "queued_at": _now(),
                "reason": (
                    "codegraph rows may carry vectors truncated by a "
                    "pre-v0.2.92 code_embed image; each exposed project's "
                    "next resync under a non-stale image re-embeds them once"
                ),
                "evidence": evidence,
            },
            "demoted": dict((existing or {}).get("demoted") or {}),
            "healed": dict((existing or {}).get("healed") or {}),
        }
        return _write_json(path, payload)


def exposure_demote_recorded(project: str) -> bool:
    """Has this project's one-time demote already run to completion?

    The expensive half of the heal is bounded SEPARATELY from the discharge:
    the discharge needs positive convergence, which a project can legitimately
    never reach, and until this existed such a project re-demoted its whole
    current-revision set — a full re-embed — on every owed resync (ship-gate
    MAJOR-3).

    Soft-fail: an unreadable/absent marker reads as "not recorded", which
    leaves the demote to the driver's own gates (owed ∧ positively-current).
    """
    marker = _read_marker()
    if marker is None:
        return False
    return str(project) in (marker.get("demoted") or {})


def record_exposure_demote(project: str) -> bool:
    """Record that ``project``'s one-time demote completed (``demoted[project]``).

    Call ONLY after a demote that reported zero per-row failures: a partial
    demote has not done the work, and the next resync must retry it (the
    demote skips rows already at the sentinel, so the retry is cheap).

    ``True`` iff the stamp is durably recorded.  No marker file ⇒ ``False``:
    nothing was owed, so nothing is recorded and no file is fabricated.
    """
    if not project:
        logger.warning(
            "code_embed_exposure: record_exposure_demote requires the project "
            "name — nothing recorded"
        )
        return False
    try:
        return bool(_record_project_stamp("demoted", project))
    except Exception as exc:  # noqa: BLE001 — bookkeeping never crashes a heal
        logger.warning("code_embed_exposure: cannot record demote: %s", exc)
        return False


def exposure_bump_owed(project: Optional[str] = None) -> bool:
    """True iff the one-time re-embed is owed.

    With ``project`` (the code-graph project name — the same string the
    demote/rows probes key on): owed iff the machine ``observed`` block
    exists AND the project has no ``healed`` entry.  This is the per-project
    one-shot: a healed project never re-demotes, and other projects' owed
    state is untouched by any single project's heal.

    ``project=None`` is the LEGACY machine-level reading, kept only so a
    not-yet-threaded consult degrades conservatively: True while the
    observation exists and NO project has been healed yet (once any project
    discharges, a no-arg consult reports False rather than claiming a
    machine-wide owed state that no longer exists).  Production consults
    (spawn owed-gate, heal gate) pass the project.

    Soft-fail: an unreadable marker reads as not-owed (a wrong spawn is
    costlier than a missed one — the marker survives for the next consult).
    """
    marker = _read_marker()
    if marker is None or marker.get("observed") is None:
        return False
    healed = marker.get("healed") or {}
    if project is None:
        return not healed
    return project not in healed


def clear_exposure_bump(project: Optional[str] = None) -> bool:
    """Record the heal for ONE project (``healed[project] = now``).

    The ``observed`` block and every other project's state are preserved —
    this is the per-project discharge that replaced the v1 whole-file
    unlink (which cleared the owed state of EVERY still-exposed project on
    the machine; register M-2 J-2).  Soft-fail; True iff the discharge is
    durably recorded (or was vacuous — no marker file at all, so nothing
    was owed).

    ``project=None`` is REFUSED (False + warning): without a project name
    the only available action would be deleting the whole marker, which is
    exactly the v1 under-heal this shape exists to prevent.

    The read-merge-write runs under :func:`_marker_lock` — see
    :func:`_record_project_stamp`, the shared home it delegates to.  Before
    that lock existed, two drivers discharging DIFFERENT projects could each
    write from a snapshot taken before the other's write, and the loser's
    project re-healed: a second demote and a second full re-embed.
    """
    if project is None:
        logger.warning(
            "code_embed_exposure: clear_exposure_bump requires the project "
            "name — refusing to clear the whole marker (other projects' "
            "owed state lives in the same file)"
        )
        return False
    try:
        recorded = _record_project_stamp("healed", project)
        if recorded is None:
            # No marker file at all — nothing was owed; vacuously cleared,
            # and no file is fabricated.
            return True
        return recorded
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_embed_exposure: cannot clear marker: %s", exc)
        return False


# ── 2b. historical stale evidence from the deferral ledger ──────────────────


def ledger_shows_truncating_image(repo_root: Any) -> bool:
    """Does the folder's UPDATE_DEFERRED ledger carry a TRUNCATING-cohort
    ``code_embed_image_stale`` entry?

    The doctor writes that entry with ``detected=`` the ``served_state``
    summary verbatim, so the pre-v0.2.92 signature in the entry text
    positively reconstructs "a truncating image was observed on this machine
    while this ledger entry was live" — covering the window where the image
    was rebuilt out-of-band after the doctor's observation but before any
    detection ran.  A mismatch-cohort entry (``served_sha`` present in its
    day) lacks the signature and is NOT evidence.  Read-only, soft-fail.
    """
    try:
        from vco_lib.deferral_report import DeferralReport

        report = DeferralReport.read(Path(repo_root))
        for entry in getattr(report, "entries", None) or []:
            if getattr(entry, "condition_id", "") != CID_IMAGE_STALE:
                continue
            text = " ".join(
                str(getattr(entry, field, "") or "")
                for field in ("title", "detected", "why_deferred")
            )
            if TRUNCATION_SIGNATURE in text:
                return True
        return False
    except Exception as exc:  # noqa: BLE001 — no ledger is "no evidence"
        logger.debug("code_embed_exposure: ledger scan skipped: %s", exc)
        return False


# ── 2c. DETECT ───────────────────────────────────────────────────────────────


def detect_and_queue(
    install_root: Any,
    repo_root: Any,
    project_name: str,
    *,
    state: Any = None,
    rows_probe: Optional[Callable[[str], Optional[bool]]] = None,
    log_event: Optional[Callable[[str, str, str], None]] = None,
) -> str:
    """Update-time exposure detection; returns one status token.

    ``state`` (an ``ImageState``) and ``rows_probe`` (``project -> True``
    rows exist at the current revision / ``False`` positively none /
    ``None`` undeterminable) are injection seams so tests describe a whole
    machine without a service, a container runtime, or a Weaviate.  In
    production ``state`` defaults to the live ``image_state(install_root)``
    probe and ``rows_probe`` to
    ``codegraph_resync.has_rows_at_current_revision``.

    Probe ORDER is deliberate: the completion evidence (a Weaviate
    aggregate on a backend the update step already talks to) runs BEFORE
    the image evidence, whose live arm is an HTTP round-trip to the
    code-embed service.  A machine with no completed graph rows — the
    overwhelmingly common non-exposed shape — then never contacts the
    service at all (cheaper in production, and no ambient-service
    dependency for suites that exercise the install maintenance flow).

    One-shot PER PROJECT: a project with a ``healed`` entry short-circuits
    BEFORE any probe (even fresh truncating evidence cannot re-fire a
    discharged project), and an already-owed project is never re-queued.
    The ``observed`` block the queue writes is machine-level — every other
    not-yet-healed project derives its owed state from it without a
    detection run of its own (its spawn owed-gate consults the same
    marker).

    Never raises for expected machine shapes; the install shim wraps the
    call anyway (a detection failure must never fail an update).
    """
    settled = _already_settled(project_name, log_event)
    if settled is not None:
        return settled

    rows = _completion_rows(project_name, rows_probe)
    if rows is None:
        # Conservative: an undeterminable completion probe NEVER fires (the
        # stale evidence persists — the live probe re-runs and the
        # observation is not consumed — so the next update decides with a
        # readable backend).
        _log(log_event, STATUS_UNDETERMINABLE, "completion probe undeterminable")
        return STATUS_UNDETERMINABLE
    if not rows:
        _log(log_event, STATUS_NOT_EXPOSED, "no rows at the current revision")
        return STATUS_NOT_EXPOSED

    stale_evidence: Optional[dict] = None
    if state is None:
        try:
            from vco_lib.code_embed_image import image_state

            state = image_state(install_root)
        except Exception as exc:  # noqa: BLE001 — could not look is not evidence
            logger.debug("code_embed_exposure: live probe skipped: %s", exc)
            state = None
    if state is not None and _is_truncating_state(state):
        stale_evidence = {
            "kind": "live_probe",
            "expected_sha": getattr(state, "expected_sha", None),
        }
    if stale_evidence is None:
        stale_evidence = historical_stale_evidence(repo_root)
    if stale_evidence is None:
        _log(log_event, STATUS_NOT_EXPOSED, "no truncating-image evidence")
        return STATUS_NOT_EXPOSED

    return _queue(project_name, stale_evidence, log_event)


def detect_from_history(
    repo_root: Any,
    project_name: str,
    *,
    rows_probe: Optional[Callable[[str], Optional[bool]]] = None,
    log_event: Optional[Callable[[str, str, str], None]] = None,
) -> str:
    """Resync-time detection for ONE project, from HISTORICAL evidence only.

    Closes the M-2 residual gap the lane reports disclosed and the owner
    ruled in scope (2026-09-22): update-time detection runs for exactly ONE
    project — the one whose maintenance step is running, normally the
    orchestrator root — and its completion evidence is that project's own
    rows.  A machine whose ROOT has no rows at the current revision (a fresh
    or moved clone with pre-existing project graphs) therefore records
    nothing, and NO project owes the heal — including the user projects
    whose rows the same truncating service corrupted.  "One-time per project"
    cannot mean "only if the root happened to have rows".

    So the project's own resync asks the question for itself.  The evidence
    it may use is the machine-level history — the persisted observation, or
    a truncating-cohort ledger entry — never a live probe: the caller (the
    heal) has just proven the image is CURRENT, so the live arm could only
    say "not truncating now", which says nothing about the rows already
    stored.

    Probe ORDER is the reverse of :func:`detect_and_queue`'s, for the same
    reason that one's is what it is — cheapest disqualifier first.  Here the
    stale arms are file reads and the completion arm is a Weaviate
    aggregate, so a machine that never saw a truncating image pays one
    ``is_file()`` and one ledger read per resync and stops.

    Returns the same status tokens as :func:`detect_and_queue`.
    """
    settled = _already_settled(project_name, log_event)
    if settled is not None:
        return settled

    stale_evidence = historical_stale_evidence(repo_root)
    if stale_evidence is None:
        _log(log_event, STATUS_NOT_EXPOSED, "no historical truncating evidence")
        return STATUS_NOT_EXPOSED

    rows = _completion_rows(project_name, rows_probe)
    if rows is None:
        _log(log_event, STATUS_UNDETERMINABLE, "completion probe undeterminable")
        return STATUS_UNDETERMINABLE
    if not rows:
        _log(log_event, STATUS_NOT_EXPOSED, "no rows at the current revision")
        return STATUS_NOT_EXPOSED

    return _queue(project_name, stale_evidence, log_event)


def historical_stale_evidence(repo_root: Any) -> Optional[dict]:
    """Machine-level evidence that a truncating image WAS serving here.

    ONE home for the two historical arms (the live probe belongs to the
    caller that can make it): the persisted observation
    ``plan_rebuild`` writes before its own rebuild erases the live fact,
    then a ``code_embed_image_stale`` ledger entry carrying the cohort
    signature.  ``None`` when neither says anything.
    """
    observation = read_stale_observation()
    if observation:
        return {"kind": "prior_observation", **observation}
    if ledger_shows_truncating_image(repo_root):
        return {"kind": "ledger_entry", "condition_id": CID_IMAGE_STALE}
    return None


def _already_settled(
    project_name: str,
    log_event: Optional[Callable[[str, str, str], None]],
) -> Optional[str]:
    """The one-shot short-circuits both detection entry points share.

    ``None`` means "not settled — go and look".  A project with a ``healed``
    entry short-circuits BEFORE any probe (even fresh truncating evidence
    cannot re-fire a discharged project), and an already-owed project is
    never re-queued.
    """
    marker = _read_marker()
    if project_name in ((marker or {}).get("healed") or {}):
        _log(log_event, STATUS_ALREADY_QUEUED,
             "project already healed — the one-shot never re-fires")
        return STATUS_ALREADY_QUEUED
    if exposure_bump_owed(project_name):
        _log(log_event, STATUS_ALREADY_QUEUED, "marker already queued")
        return STATUS_ALREADY_QUEUED
    return None


def _completion_rows(
    project_name: str,
    rows_probe: Optional[Callable[[str], Optional[bool]]],
) -> Optional[bool]:
    """The completion evidence, tri-state (see
    ``codegraph_resync.has_rows_at_current_revision`` for why rows AT the
    current revision are the signal).  A probe that RAISED is undeterminable,
    never "no"."""
    if rows_probe is None:
        from vco_lib.codegraph_resync import has_rows_at_current_revision

        rows_probe = has_rows_at_current_revision
    try:
        return rows_probe(project_name)
    except Exception as exc:  # noqa: BLE001 — a probe failure is undeterminable
        logger.debug("code_embed_exposure: rows probe raised: %s", exc)
        return None


def _queue(
    project_name: str,
    stale_evidence: dict,
    log_event: Optional[Callable[[str, str, str], None]],
) -> str:
    """Write the machine-level ``observed`` block and absorb the evidence.

    ONE home for the queue half of both detection entry points, so the
    consume-on-queue hygiene and the user-facing line cannot drift apart.
    """
    if not queue_exposure_bump({
        "stale": stale_evidence,
        "completion": {
            "kind": "rows_at_current_revision",
            "project": project_name,
        },
    }):
        _log(log_event, STATUS_ALREADY_QUEUED, "marker raced in")
        return STATUS_ALREADY_QUEUED
    consume_stale_observation()
    print(
        "  → code-embed truncation exposure detected — a ONE-TIME re-embed is "
        "queued per exposed project; each fires on that project's next "
        "code-graph resync under a non-stale image (never through the "
        "truncating one)"
    )
    _log(log_event, STATUS_QUEUED, f"evidence={stale_evidence.get('kind')}")
    return STATUS_QUEUED


def _log(
    log_event: Optional[Callable[[str, str, str], None]],
    status: str,
    detail: str,
) -> None:
    if log_event is None:
        return
    try:
        log_event("code_embed_exposure", "ok", f"{status}: {detail}")
    except Exception:  # noqa: BLE001 — logging never gates detection
        pass
