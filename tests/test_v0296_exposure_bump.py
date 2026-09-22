# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 M-2: the exposure-conditional ONE-TIME embed_revision bump.

Register issue 8 (2026-09-20): rows embedded through a pre-v0.2.92
code-embed image were silently truncated at HTTP 200.  Their content hashes
are CORRECT — truncation corrupts vectors, not text — so every hash/revision
gate skips them forever.  WP-3 blocked NEW resyncs from a stale image; this
lane heals the historical damage, owner-decided shape:

* EXPOSURE DETECTION at update time (install.py's codegraph-maintenance
  step → the thin ``code_embed_exposure.detect_and_queue`` call):
  truncating-image evidence (live probe | persisted observation | ledger
  entry) AND completion evidence (rows at the current embed revision) →
  the marker's machine-level ``observed`` block.  No positive evidence →
  nothing fires.
* HEAL: the next resync under a POSITIVELY current image (chained behind
  the WP-3 gate) demotes current-revision rows to the vectorless sentinel
  so the EXISTING revision gate re-embeds exactly them once, then records
  ``healed[project]`` on positive convergence.  One-shot PER PROJECT —
  owed = observed ∧ no healed entry — idempotent, resumable: the first
  project's heal never discharges another's owed state (register M-2 J-2
  follow-up: the v1 machine-level clear stranded every project after the
  first on multi-project installs).
* NON-EXPOSED machines: byte-for-byte unchanged — the hash gate stays
  authoritative, zero extra embeds.
* The CLI remedy text tells the truth (rebuild first; the exposure bump
  heals; ``--force-recreate`` is the unconditional escape) — the pre-fix
  text promised a plain re-run of the resync would heal, which hash-skips
  exactly the corrupted rows.

Hermeticity: the state dir is pinned per test (``VCT_STATE_DIR``), verdicts
and probes are injected through the module seams, and no test contacts a
service or a Weaviate.
"""

from __future__ import annotations

import json
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import code_embed_exposure as ce  # noqa: E402
from vco_lib import code_embed_image as cei  # noqa: E402
from vco_lib import codegraph_resync as cr  # noqa: E402


# ───────────────────────── shared fake world ─────────────────────────


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Pin the machine-level state home; return the state root."""
    root = tmp_path / "vct"
    root.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(root))
    return root


def _image_state(verdict: str, *, served_sha=None, expected_sha="e" * 12):
    return cei.ImageState(
        verdict=verdict,
        summary=f"code_embed: {verdict}",
        expected_sha=expected_sha,
        served_sha=served_sha,
    )


_TRUNCATING = lambda: _image_state(  # noqa: E731 — readable inline
    cei.STALE, served_sha=None,
)
_MISMATCH = lambda: _image_state(  # noqa: E731
    cei.STALE, served_sha="a" * 12,
)


class _RunRecord:
    """subprocess.run stand-in preserving the driver's historical seam."""

    def __init__(self, returncode=0):
        self.calls = []
        self._rc = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return types.SimpleNamespace(returncode=self._rc)


class _SpawnRecord:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return types.SimpleNamespace(pid=4321)


def _analyzer_at(tmp_path: Path) -> Path:
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    analyzer = scripts / "analyze_code_graph.py"
    analyzer.write_text("# stub analyzer\n", encoding="utf-8")
    return analyzer


def _set_verdict(monkeypatch, verdict: str):
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: verdict)


def _quiet_driver_probes(monkeypatch, *, stale_sequence=None):
    """Silence the driver's non-heal machinery; stale_sequence feeds the
    pre-walk then post-walk count_stale_rows calls."""
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: None)
    seq = list(stale_sequence or [])
    monkeypatch.setattr(
        cr, "count_stale_rows",
        lambda *a, **k: seq.pop(0) if seq else {},
    )


# ─────────── Weaviate fakes for the two row-side probes ───────────


class _FakeColl:
    """Collection fake for the M-2 probes: aggregate.over_all(filtered
    total_count), iterator(return_properties), data.update."""

    def __init__(self, name, rows, *, agg_raises=False, fail_uuids=()):
        self.name = name
        self._rows = list(rows)  # [(uuid, rev)] — rev may be None
        self._agg_raises = agg_raises
        self._fail_uuids = set(fail_uuids)
        self.updates = []
        self.aggregate = types.SimpleNamespace(over_all=self._over_all)
        self.data = types.SimpleNamespace(update=self._update)

    def _over_all(self, filters=None, total_count=False, **_kw):
        if self._agg_raises:
            raise RuntimeError("aggregate unsupported (injected)")
        want = getattr(filters, "value", None)
        n = sum(1 for _u, rev in self._rows if rev == want)
        return types.SimpleNamespace(total_count=n)

    def iterator(self, return_properties=None, **_kw):
        for uid, rev in self._rows:
            yield types.SimpleNamespace(
                uuid=uid, properties={"embed_revision": rev},
            )

    def _update(self, uuid=None, properties=None, **_kw):
        if uuid in self._fail_uuids:
            raise RuntimeError("update refused (injected)")
        self.updates.append((uuid, dict(properties or {})))


class _FakeCollections:
    def __init__(self, colls):
        self._colls = colls

    def exists(self, name):
        return name in self._colls

    def get(self, name):
        return self._colls[name]


class _FakeClient:
    def __init__(self, colls):
        self.collections = _FakeCollections(colls)
        self.closed = False

    def close(self):
        self.closed = True


def _probe_world(monkeypatch, colls_by_base):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    client = _FakeClient({
        f"P_{base}": coll for base, coll in colls_by_base.items()
    })
    return client


# ───────────────── 1. marker primitives (per-project one-shot) ─────────────────


def test_queue_is_idempotent_and_keeps_first_evidence(state_dir):
    assert ce.queue_exposure_bump({"stale": {"kind": "live_probe"}}) is True
    marker = state_dir / ce.MARKER_REL
    assert marker.is_file()
    first = json.loads(marker.read_text(encoding="utf-8"))
    # A re-queue NEVER overwrites — the original evidence/queued_at stand.
    assert ce.queue_exposure_bump({"stale": {"kind": "ledger_entry"}}) is False
    assert json.loads(marker.read_text(encoding="utf-8")) == first
    assert ce.exposure_bump_owed("MyProj") is True
    assert ce.clear_exposure_bump("MyProj") is True
    assert ce.exposure_bump_owed("MyProj") is False
    # The discharge is an ENTRY, not a deletion: observed stays on disk for
    # every not-yet-healed sibling project.
    after = json.loads(marker.read_text(encoding="utf-8"))
    assert after["observed"] == first["observed"]
    assert "MyProj" in after["healed"]


def test_observe_persists_only_the_truncating_cohort(state_dir):
    ce.observe_stale_image(_MISMATCH())  # served_sha present → refuses loudly
    assert ce.read_stale_observation() is None
    ce.observe_stale_image(_image_state(cei.CURRENT))
    assert ce.read_stale_observation() is None
    ce.observe_stale_image(_TRUNCATING())
    obs = ce.read_stale_observation()
    assert obs is not None and obs["served_sha"] is None
    # First-wins: a later observation does not replace the earliest evidence.
    first = dict(obs)
    ce.observe_stale_image(_TRUNCATING())
    assert ce.read_stale_observation() == first
    ce.consume_stale_observation()
    assert ce.read_stale_observation() is None


def test_plan_rebuild_observes_a_live_truncating_image(state_dir, tmp_path):
    """The installer's compose-up decision is the OBSERVE site: on an owned
    machine the rebuild it plans erases the live evidence before the same
    update's maintenance step can probe it."""
    plan = cei.plan_rebuild(
        decisions=None, has_gpu=True, force_separate=False,
        install_root=tmp_path, url=None,
        services_to_start=(), services_to_recreate=(),
        adopt_action="adopt", managed_probe="managed",
        state=_TRUNCATING(),
    )
    assert plan.build is True
    assert ce.read_stale_observation() is not None

    ce.consume_stale_observation()
    cei.plan_rebuild(
        decisions=None, has_gpu=True, force_separate=False,
        install_root=tmp_path, url=None,
        services_to_start=(), services_to_recreate=(),
        adopt_action="adopt", managed_probe="managed",
        state=_MISMATCH(),
    )
    assert ce.read_stale_observation() is None, (
        "a digest-MISMATCH image refuses loudly (v0.2.92 server.py) — it is "
        "not silent-truncation evidence and must not queue a heal"
    )


# ───────────────────── 2. the exposure matrix (detect) ─────────────────────


def test_stale_now_plus_completion_queues_marker(state_dir, tmp_path, capsys):
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_TRUNCATING(), rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_QUEUED
    assert ce.exposure_bump_owed("MyProj")
    marker = json.loads(
        (state_dir / ce.MARKER_REL).read_text(encoding="utf-8")
    )
    assert marker["observed"]["evidence"]["stale"]["kind"] == "live_probe"
    assert marker["observed"]["evidence"]["completion"]["project"] == "MyProj"
    assert marker["healed"] == {}
    out = capsys.readouterr().out
    assert "ONE-TIME" in out  # the update output names the queued heal


def test_queued_marker_does_not_embed_while_stale(state_dir, tmp_path, monkeypatch):
    """Queueing is passive: with the image still stale, the WP-3 spawn arm
    refuses the walk and the marker SURVIVES (no embed through the
    truncating service — the heal waits for a non-stale run)."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    _set_verdict(monkeypatch, "stale")
    popen = _SpawnRecord()
    monkeypatch.setattr(cr.subprocess, "Popen", popen)
    _analyzer_at(tmp_path)

    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe=sys.executable, check_owed=False,
    )
    assert result.status == "deferred"
    assert popen.calls == []
    assert ce.exposure_bump_owed(), "the stale refusal must NOT consume the bump"


def test_stale_only_without_completion_evidence_queues_nothing(
    state_dir, tmp_path,
):
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_TRUNCATING(), rows_probe=lambda name: False,
    )
    assert status == ce.STATUS_NOT_EXPOSED
    assert not ce.exposure_bump_owed()


def test_undeterminable_completion_probe_never_fires(state_dir, tmp_path):
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_TRUNCATING(), rows_probe=lambda name: None,
    )
    assert status == ce.STATUS_UNDETERMINABLE
    assert not ce.exposure_bump_owed()


def test_clean_image_plus_completion_queues_nothing(state_dir, tmp_path):
    """THE non-exposed pin: a current image, completed rows, no observation,
    no ledger entry → nothing is written and the hash gate stays
    authoritative (zero extra embeds)."""
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_image_state(cei.CURRENT, served_sha="e" * 12),
        rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_NOT_EXPOSED
    assert not ce.exposure_bump_owed()
    assert not (state_dir / ce.MARKER_REL).exists()
    assert not (state_dir / ce.OBSERVATION_REL).exists()


def test_digest_mismatch_cohort_is_not_exposure(state_dir, tmp_path):
    """A STALE verdict WITH a served digest is a post-v0.2.92 image: it
    REFUSES over-window input at HTTP 400, its failed embeds land as
    vectorless rows the existing gate already re-embeds.  Firing the
    one-time bump there would burn a full redundant re-embed."""
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_MISMATCH(), rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_NOT_EXPOSED
    assert not ce.exposure_bump_owed()


def test_prior_observation_is_historical_evidence(state_dir, tmp_path):
    """The owned-machine ordering: compose-up OBSERVED the truncating image
    and rebuilt it; by maintenance time the live probe reads CURRENT.  The
    persisted observation is the positive reconstruction — and queuing
    CONSUMES it so it can never re-fire a healed machine."""
    ce.observe_stale_image(_TRUNCATING())
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_image_state(cei.CURRENT, served_sha="e" * 12),
        rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_QUEUED
    assert ce.exposure_bump_owed()
    assert ce.read_stale_observation() is None


def test_ledger_entry_with_truncation_signature_is_evidence(
    state_dir, tmp_path, monkeypatch,
):
    from vco_lib.deferral_emit import emit
    from vco_lib.deferral_report import DeferralEntry

    entry = DeferralEntry(
        condition_id="code_embed_image_stale",
        title="code-embedding service runs an image older than its source",
        detected=(
            "code_embed: the running service predates v0.2.92 (its /health "
            "reports no source_sha). It still TRUNCATES over-window input "
            "silently at HTTP 200 instead of refusing it."
        ),
        why_deferred="rebuild is the user's run",
        command_to_apply="python install.py --update",
        severity="warning",
        kg_node_refs=[],
    )
    emit(tmp_path, entry)
    assert ce.ledger_shows_truncating_image(tmp_path) is True

    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_image_state(cei.CURRENT, served_sha="e" * 12),
        rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_QUEUED


def _doctor_stale_entry(tmp_path: Path, health: dict):
    """The REAL doctor pipeline for a `code_embed_image_stale` entry:
    `served_state` (pure) → `probe_code_embed_image` (the finding) →
    `_code_embed_image_entry` (the DeferralEntry the user reads).

    Nothing is hand-written: that is the whole point of the pin below — a
    fixture entry proves only what the fixture's author believed.
    """
    from vco_lib import doctor

    root = tmp_path / "orchestrator-root"
    (root / "vco_lib").mkdir(parents=True)
    (root / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")
    state = cei.served_state("e" * 12, health)
    res = doctor.DoctorResolvers(
        code_embed_state=lambda _root: state,
        code_embed_rebuild_context=lambda: None,
    )
    findings = doctor.probe_code_embed_image(root, res, {})
    assert len(findings) == 1 and findings[0].condition_id == ce.CID_IMAGE_STALE
    return doctor._code_embed_image_entry(findings[0])


def test_the_doctors_SHARED_text_keeps_the_mismatch_cohort_out(
    state_dir, tmp_path,
):
    """MINOR-2, verified in source and pinned through the REAL builder.

    `ledger_shows_truncating_image` scans title + detected + why_deferred for
    `TRUNCATION_SIGNATURE`. Only `detected` is cohort-specific (it carries
    the `served_state` summary verbatim); the doctor's title AND why_deferred
    are SHARED by both cohorts, and the why_deferred already discusses
    truncation — "A **pre-v0.2.92** image TRUNCATES over-window code…".

    That is one rephrasing away from matching: were the shared text to say
    "predates v0.2.92", a DIGEST-MISMATCH entry would become truncating-cohort
    evidence, and a machine whose image provably REFUSES over-window input
    (it carries v0.2.92's server) would pay a full redundant re-embed. So the
    two strings must NOT be "aligned" — they are a discriminator and a
    sentence, and this pins the discriminator's freedom from the sentence.
    """
    from vco_lib.deferral_emit import emit

    mismatch = _doctor_stale_entry(
        tmp_path, {"status": "ok", "source_sha": "a" * 12},
    )
    # The shared prose IS about truncation — that is what makes the pin real.
    assert "pre-v0.2.92" in mismatch.why_deferred
    assert ce.TRUNCATION_SIGNATURE not in mismatch.why_deferred
    assert ce.TRUNCATION_SIGNATURE not in mismatch.title

    emit(tmp_path, mismatch)
    assert ce.ledger_shows_truncating_image(tmp_path) is False, (
        "a provably-refusing (post-v0.2.92) image must never be read as "
        "truncating-cohort evidence"
    )


def test_the_doctors_TRUNCATING_entry_is_evidence_through_the_real_builder(
    state_dir, tmp_path,
):
    """The act half of the same decision, so the pin cannot pass by being
    blind to both cohorts: the truncating entry the doctor really emits DOES
    carry the signature and IS evidence."""
    from vco_lib.deferral_emit import emit

    truncating = _doctor_stale_entry(tmp_path, {"status": "ok", "dim": 2048})
    assert ce.TRUNCATION_SIGNATURE in truncating.detected

    emit(tmp_path, truncating)
    assert ce.ledger_shows_truncating_image(tmp_path) is True


def test_the_OTHER_stale_image_emitter_is_never_truncating_evidence(
    state_dir, tmp_path,
):
    """The second emitter, pinned through its REAL builder (ship-gate
    re-review, 2026-09-22).

    `codegraph_resync.build_stale_image_deferral` is the WP-3 gate's own
    stale-image entry. It must NOT be read as truncating-cohort evidence,
    and the reason is a fact about the gate rather than about the text: the
    verdict it acts on is `stale`, which covers BOTH cohorts, so the entry
    cannot say whether the image truncates silently (pre-v0.2.92) or refuses
    loudly (a post-v0.2.92 digest mismatch). Firing the one-time re-embed on
    the second would cost a healthy machine a full redundant re-embed.

    TWO independent guards keep it out, and both are pinned here because
    either alone is one edit from disappearing: it carries a DIFFERENT
    condition id, and its text does not contain the cohort signature. The
    cohort-specific evidence lives in the doctor's `code_embed_image_stale`
    entry, which is scanned and is not affected by this.
    """
    from vco_lib.deferral_emit import emit

    entry = cr.build_stale_image_deferral("MyProj", "python install.py --update")
    assert entry is not None
    assert entry.condition_id == "codegraph_embed_resync_pending"
    assert entry.condition_id != ce.CID_IMAGE_STALE
    blob = " ".join([entry.title, entry.detected, entry.why_deferred])
    assert ce.TRUNCATION_SIGNATURE not in blob, (
        "the gate knows only 'stale' — naming the truncating cohort here "
        "would make a provably-refusing image queue a full re-embed"
    )

    emit(tmp_path, entry)
    assert ce.ledger_shows_truncating_image(tmp_path) is False
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_image_state(cei.CURRENT, served_sha="e" * 12),
        rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_NOT_EXPOSED, (
        "the WP-3 gate's own entry must not queue the one-time re-embed"
    )

    # The condition-id gate on its own: were this entry's text ever reworded
    # to name the cohort (it is about the same defect, so a future editor
    # plausibly would), the scan must STILL ignore it.
    from vco_lib.deferral_report import DeferralEntry

    emit(tmp_path, DeferralEntry(
        condition_id=entry.condition_id,
        title=entry.title,
        detected=(
            "The running code-embedding service predates v0.2.92 and "
            "truncates over-window input silently."
        ),
        why_deferred=entry.why_deferred,
        command_to_apply=entry.command_to_apply,
        severity="warning",
        kg_node_refs=[],
    ))
    assert ce.ledger_shows_truncating_image(tmp_path) is False, (
        "the scan must key on the DOCTOR's cohort-specific entry, not on any "
        "entry that happens to mention the cohort"
    )


def test_ledger_entry_without_signature_is_not_evidence(state_dir, tmp_path):
    from vco_lib.deferral_emit import emit
    from vco_lib.deferral_report import DeferralEntry

    entry = DeferralEntry(
        condition_id="code_embed_image_stale",
        title="code-embedding service runs an image older than its source",
        detected=(
            "code_embed: the running service is built from OLDER source than "
            "this checkout (image abc, source def)."
        ),
        why_deferred="rebuild is the user's run",
        command_to_apply="python install.py --update",
        severity="warning",
        kg_node_refs=[],
    )
    emit(tmp_path, entry)
    assert ce.ledger_shows_truncating_image(tmp_path) is False
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_image_state(cei.CURRENT, served_sha="e" * 12),
        rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_NOT_EXPOSED


def test_detection_with_existing_marker_is_a_noop(state_dir, tmp_path):
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    before = (state_dir / ce.MARKER_REL).read_bytes()
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_TRUNCATING(), rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_ALREADY_QUEUED
    assert (state_dir / ce.MARKER_REL).read_bytes() == before


# ───────────────── 3. the heal (driver) — one-shot state machine ─────────────────


def _driver_world(monkeypatch, tmp_path, *, verdict="current", demote=None,
                  stale_sequence=None):
    _set_verdict(monkeypatch, verdict)
    _quiet_driver_probes(monkeypatch, stale_sequence=stale_sequence)
    demote_calls = []

    def _demote(project_name, **kwargs):
        demote_calls.append(project_name)
        return demote if demote is not None else {"demoted": 7, "failed": 0}

    monkeypatch.setattr(cr, "demote_current_revision_rows", _demote)
    run = _RunRecord(returncode=0)
    monkeypatch.setattr(cr.subprocess, "run", run)
    return run, demote_calls


def test_driver_heals_once_then_clears_marker(state_dir, tmp_path, monkeypatch):
    """THE remedy pin: marker owed + non-stale image → the driver demotes
    (making every current-revision row revision-OWED for the existing gate),
    runs the walk, and on positive convergence clears the marker.  A SECOND
    run re-embeds nothing — one-shot."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    run, demote_calls = _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 7}, {}],  # pre-walk owed → converged
    )
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert rc == 0
    assert demote_calls == ["MyProj"], "the heal demotes exactly this project"
    assert len(run.calls) == 1, "the walk must run (it performs the re-embed)"
    assert not ce.exposure_bump_owed(), "positive convergence clears the marker"

    # Second pass: no marker → no demote, walk proceeds unchanged.
    run2, demote2 = _driver_world(monkeypatch, tmp_path, stale_sequence=[{}, {}])
    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert rc == 0
    assert demote2 == []
    assert len(run2.calls) == 1
    assert not ce.exposure_bump_owed()


def test_second_detection_after_clear_does_not_refire(state_dir, tmp_path):
    """Idempotency, detection side: after the heal cleared the marker, a
    detection pass over a machine whose image is now current (the heal's own
    precondition) with the observation consumed finds no evidence."""
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "MyProj",
        state=_image_state(cei.CURRENT, served_sha="e" * 12),
        rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_NOT_EXPOSED
    assert not ce.exposure_bump_owed()


def test_marker_plus_stale_image_still_blocked_marker_survives(
    state_dir, tmp_path, monkeypatch,
):
    """WP-3 interplay: the heal is CHAINED behind the stale gate — a stale
    image blocks the walk entirely and the marker survives untouched."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    run, demote_calls = _driver_world(monkeypatch, tmp_path, verdict="stale")
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert rc == 0
    assert run.calls == [], "no analyzer under a stale image"
    assert demote_calls == [], "no demote under a stale image"
    assert ce.exposure_bump_owed()


def test_unknown_verdict_keeps_marker_owed(state_dir, tmp_path, monkeypatch):
    """The heal is STRICTER than the WP-3 walk gate: `unknown` lets an
    ordinary walk proceed (it re-embeds nothing), but the heal re-embeds
    EVERYTHING — never through a service whose freshness could not be
    proven.  Marker survives for the next run."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    run, demote_calls = _driver_world(
        monkeypatch, tmp_path, verdict="unknown", stale_sequence=[{}, {}],
    )
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert rc == 0
    assert len(run.calls) == 1, "unknown keeps the ordinary walk behavior"
    assert demote_calls == []
    assert ce.exposure_bump_owed()


def test_partial_demote_failure_keeps_marker(state_dir, tmp_path, monkeypatch):
    """Clearing the marker over un-demoted rows would silently under-heal
    (they hash-skip the walk and stay truncated) — any demote failure keeps
    the bump owed even when the walk converged."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    _driver_world(
        monkeypatch, tmp_path,
        demote={"demoted": 6, "failed": 1},
        stale_sequence=[{"P_CodeFunction": 6}, {}],
    )
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert rc == 0
    assert ce.exposure_bump_owed()


def test_undeterminable_demote_keeps_marker(state_dir, tmp_path, monkeypatch):
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    _driver_world(monkeypatch, tmp_path, demote=None, stale_sequence=[{}, {}])
    # demote=None makes the fake return the default — override explicitly:
    monkeypatch.setattr(cr, "demote_current_revision_rows", lambda *a, **k: None)
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert rc == 0
    assert ce.exposure_bump_owed()


def test_unconverged_walk_keeps_marker(state_dir, tmp_path, monkeypatch):
    """No positive convergence → no clear.  The demoted rows stay owed, so
    the NEXT resync resumes the heal (resumable by construction)."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 7}, {"P_CodeFunction": 2}],
    )
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert rc == 0
    assert ce.exposure_bump_owed()


def test_a_project_that_never_converges_demotes_only_ONCE(
    state_dir, tmp_path, monkeypatch,
):
    """SHIP-GATE MAJOR-3 (2026-09-22): "ONE-TIME" must bound the DEMOTE, not
    just the discharge.

    The clear needs a GLOBAL post-walk stale count of zero, and a project can
    legitimately never reach it — the driver has a whole NO-PROGRESS branch and
    `list_owed_row_identities` to diagnose exactly that state.  While the demote
    repeated on every owed resync, such a project re-demoted its ENTIRE
    current-revision set and re-embedded the whole graph (GPU-hours) on every
    single update, forever — the opposite of what
    `code_embed_exposure`'s docstring ("ONE-TIME", "each heals once") and the
    CLI remedy text ("queues a ONE-TIME re-embed") promise.
    """
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    analyzer = _analyzer_at(tmp_path)

    # Pass 1: demote succeeds, the walk leaves stuck rows → no discharge.
    _run1, demote1 = _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 9}, {"P_CodeFunction": 2}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote1 == ["MyProj"], "the first owed resync performs the demote"
    assert ce.exposure_bump_owed("MyProj"), "unconverged keeps the bump owed"

    # Pass 2 — the next owed resync on the same never-converging project.
    _run2, demote2 = _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 2}, {"P_CodeFunction": 2}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote2 == [], (
        "the one-time demote must NOT repeat: re-demoting the current-revision "
        "set re-embeds the whole graph on every update"
    )
    # Pass 3, for good measure: still no second demote.
    _run3, demote3 = _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 2}, {"P_CodeFunction": 2}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote3 == []


def test_a_LATE_convergence_still_discharges_the_recorded_demote(
    state_dir, tmp_path, monkeypatch,
):
    """The leave-alone half of MAJOR-3: making the demote one-shot must not
    strand the project OWED forever.  A project that demoted in an earlier
    pass and converges in a later one discharges then — the discharge asks
    whether the demote HAPPENED, not whether it happened in THIS run."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    analyzer = _analyzer_at(tmp_path)

    _run1, demote1 = _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 9}, {"P_CodeFunction": 2}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote1 == ["MyProj"]
    assert ce.exposure_bump_owed("MyProj")

    # Later pass: no second demote, and this time the walk converges.
    _run2, demote2 = _driver_world(
        monkeypatch, tmp_path, stale_sequence=[{"P_CodeFunction": 2}, {}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote2 == []
    assert not ce.exposure_bump_owed("MyProj"), (
        "convergence over an already-demoted project is the discharge"
    )


def test_an_unknown_verdict_still_blocks_the_discharge_after_a_demote(
    state_dir, tmp_path, monkeypatch,
):
    """Heal freshness stays STRICTER than the walk gate on every pass, not
    only the demoting one: a later walk under an unproven image re-embeds the
    demoted rows through a service whose freshness nobody could confirm, so it
    must not be allowed to declare the project healed."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    analyzer = _analyzer_at(tmp_path)

    _run1, demote1 = _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 9}, {"P_CodeFunction": 2}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote1 == ["MyProj"]

    _run2, demote2 = _driver_world(
        monkeypatch, tmp_path, verdict="unknown",
        stale_sequence=[{"P_CodeFunction": 2}, {}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote2 == []
    assert ce.exposure_bump_owed("MyProj"), (
        "an unproven image cannot discharge the one-time heal"
    )


def test_a_partially_failed_demote_is_RETRIED_on_the_next_pass(
    state_dir, tmp_path, monkeypatch,
):
    """The other leave-alone case: only a FULLY successful demote is
    one-shot.  A pass that left rows un-demoted (per-row update failures) has
    not done the work, so the next owed resync must demote again — the
    function skips rows already at the sentinel, so the retry only touches
    the remainder."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    analyzer = _analyzer_at(tmp_path)

    _run1, demote1 = _driver_world(
        monkeypatch, tmp_path,
        demote={"demoted": 6, "failed": 1},
        stale_sequence=[{"P_CodeFunction": 7}, {"P_CodeFunction": 1}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote1 == ["MyProj"]
    assert ce.exposure_bump_owed("MyProj")

    _run2, demote2 = _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 1}, {}],
    )
    cr.run_resync_and_verify("MyProj", tmp_path, analyzer)
    assert demote2 == ["MyProj"], "an incomplete demote is not the one-shot"
    assert not ce.exposure_bump_owed("MyProj")


def test_a_project_whose_exposure_NOBODY_detected_still_heals(
    state_dir, tmp_path, monkeypatch,
):
    """M-2 residual gap (owner ruled IN SCOPE, 2026-09-22): update-time
    detection runs for ONE project — normally the orchestrator root — and
    keys its completion evidence on THAT project's rows.  A root with no rows
    at the current revision (a fresh or moved clone whose user projects
    already have graphs) records nothing, so NO marker exists and no project
    owes the heal — while the same machine-level truncating service corrupted
    every project's rows alike.

    The surviving machine-level evidence (the observation `plan_rebuild`
    persisted before its own rebuild) is enough for the project's OWN resync
    to answer the question for itself.
    """
    # The machine saw the truncating image; the root's detection found no
    # rows, so it queued nothing and the observation was NOT consumed.
    ce.observe_stale_image(_TRUNCATING())
    assert ce.read_stale_observation() is not None
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "TheRoot",
        state=_TRUNCATING(), rows_probe=lambda name: False,
    )
    assert status == ce.STATUS_NOT_EXPOSED
    assert not ce.exposure_bump_owed("MyProj"), "nobody owes anything yet"

    # MyProj's own resync: rows at the current revision + the surviving
    # observation → it detects its own exposure and heals.
    monkeypatch.setattr(cr, "has_rows_at_current_revision", lambda *a, **k: True)
    run, demote_calls = _driver_world(
        monkeypatch, tmp_path,
        stale_sequence=[{"P_CodeFunction": 7}, {}],
    )
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)

    assert rc == 0
    assert demote_calls == ["MyProj"], (
        "the project's own resync must detect and heal its exposure"
    )
    assert len(run.calls) == 1
    assert not ce.exposure_bump_owed("MyProj"), "converged → discharged"


def test_a_machine_with_NO_historical_evidence_never_self_detects(
    state_dir, tmp_path, monkeypatch,
):
    """The leave-alone half: on a machine that never saw a truncating image
    the resync-time detection must find nothing — no marker, no demote, and
    (the cheap-disqualifier-first ordering) not even a completion probe."""
    def _rows(*_a, **_k):  # pragma: no cover — the assertion IS the test
        raise AssertionError(
            "no historical evidence ⇒ the rows aggregate must not be paid"
        )

    monkeypatch.setattr(cr, "has_rows_at_current_revision", _rows)
    run, demote_calls = _driver_world(
        monkeypatch, tmp_path, stale_sequence=[{}, {}],
    )
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)

    assert rc == 0
    assert demote_calls == []
    assert len(run.calls) == 1, "the ordinary walk is untouched"
    assert not ce.exposure_bump_owed("MyProj")
    assert not (state_dir / "state" / "code_embed_exposure_bump.json").exists()


def test_a_healed_project_never_self_detects_again(
    state_dir, tmp_path, monkeypatch,
):
    """And a project that already healed stays healed even if the machine's
    historical evidence is still lying around: the one-shot short-circuit
    runs BEFORE any evidence is consulted."""
    ce.observe_stale_image(_TRUNCATING())
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    assert ce.clear_exposure_bump("MyProj")
    # Put the observation back: a leftover file must not resurrect a heal.
    ce.observe_stale_image(_TRUNCATING())

    run, demote_calls = _driver_world(
        monkeypatch, tmp_path, stale_sequence=[{}, {}],
    )
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)

    assert rc == 0
    assert demote_calls == []
    assert not ce.exposure_bump_owed("MyProj")


# ───────────────── 4. the spawn gate honours the marker ─────────────────


def test_spawn_marker_overrides_zero_stale_not_owed(
    state_dir, tmp_path, monkeypatch,
):
    """The corrupted rows are hash- AND revision-CURRENT by construction —
    the owed probe says not_owed on exactly the machine that owes the heal.
    The marker overrides the short-circuit; without it, behavior is
    byte-for-byte the pre-M-2 not_owed."""
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    _set_verdict(monkeypatch, "current")
    monkeypatch.setattr(
        cr, "count_stale_rows",
        lambda *a, **k: {"P_CodeModule": 0, "P_CodeClass": 0, "P_CodeFunction": 0},
    )
    monkeypatch.setattr(cr, "count_cleanup_owed_rows", lambda *a, **k: 0)
    popen = _SpawnRecord()
    monkeypatch.setattr(cr.subprocess, "Popen", popen)
    _analyzer_at(tmp_path)

    # Control: no marker → not_owed (zero extra embeds on a clean machine).
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe=sys.executable,
    )
    assert result.status == "not_owed"
    assert popen.calls == []

    # Act: marker queued → the spawn happens despite the positive zero.
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe=sys.executable,
    )
    assert result.status == "launched"
    driver_calls = [c for c in popen.calls if "--run-resync" in c["argv"]]
    assert len(driver_calls) == 1


# ───────────────── 5. the row-side probes (unit, fake client) ─────────────────


def test_has_rows_at_current_revision_tri_state(monkeypatch):
    client = _probe_world(monkeypatch, {
        "CodeModule": _FakeColl("P_CodeModule", [("u1", 1), ("u2", 0)]),
        "CodeClass": _FakeColl("P_CodeClass", [("u3", None)]),
        "CodeFunction": _FakeColl("P_CodeFunction", []),
    })
    assert cr.has_rows_at_current_revision(
        "MyProj", current_revision=1, client=client,
    ) is True

    client = _probe_world(monkeypatch, {
        "CodeModule": _FakeColl("P_CodeModule", [("u1", 0), ("u2", None)]),
        "CodeClass": _FakeColl("P_CodeClass", []),
        "CodeFunction": _FakeColl("P_CodeFunction", [("u3", 0)]),
    })
    assert cr.has_rows_at_current_revision(
        "MyProj", current_revision=1, client=client,
    ) is False

    client = _probe_world(monkeypatch, {
        "CodeModule": _FakeColl("P_CodeModule", [], agg_raises=True),
    })
    assert cr.has_rows_at_current_revision(
        "MyProj", current_revision=1, client=client,
    ) is None, "an unprobeable collection is undeterminable, never False"


def test_demote_touches_only_current_revision_rows(monkeypatch):
    mod = _FakeColl("P_CodeModule", [
        ("u1", 1), ("u2", 1), ("u3", 0), ("u4", None),
    ])
    fn = _FakeColl(
        "P_CodeFunction", [("f1", 1), ("f2", 1)], fail_uuids={"f2"},
    )
    client = _probe_world(monkeypatch, {
        "CodeModule": mod,
        "CodeClass": _FakeColl("P_CodeClass", []),
        "CodeFunction": fn,
    })
    result = cr.demote_current_revision_rows(
        "MyProj", current_revision=1, client=client,
    )
    assert result == {"demoted": 3, "failed": 1}
    # Only the rows AT the current revision were demoted, to the sentinel.
    assert mod.updates == [
        ("u1", {"embed_revision": 0}), ("u2", {"embed_revision": 0}),
    ]
    assert fn.updates == [("f1", {"embed_revision": 0})]
    # A caller-supplied client is NOT closed by the probe (owner closes it).
    assert client.closed is False


# ───────────────── 6. the remedy text tells the truth ─────────────────


def test_cli_stale_remedy_text_is_true(monkeypatch, tmp_path, capsys):
    """Pin (M-2 remedy-text fix): the stale CLI verdict must lead with
    REBUILD-FIRST, name the exposure-conditional one-time re-embed as the
    heal for rows embedded while stale, keep --force-recreate as the
    unconditional escape — and must NOT carry the pre-fix promise that a
    plain re-run of the resync heals (it hash-skips exactly those rows)."""
    monkeypatch.setattr(cei, "image_state", lambda *a, **k: _TRUNCATING())
    rc = cei._main(["--root", str(tmp_path)])
    assert rc == cei.EXIT_BY_VERDICT[cei.STALE]
    out = capsys.readouterr().out
    assert "REBUILD the image FIRST" in out
    assert "NOT healed by a plain resync" in out
    assert "ONE-TIME" in out
    assert "code_embed_exposure" in out
    assert "--force-recreate" in out
    assert "THEN re-run the code-graph" not in out, (
        "the pre-fix false promise must not come back: a plain resync "
        "hash-skips exactly the rows it claims to heal"
    )


def test_truncation_signature_matches_served_state_text():
    """The ledger scan keys on the served_state summary's cohort signature —
    a text drift there would silently disable the historical evidence arm."""
    state = cei.served_state("e" * 12, {"status": "ok"})  # no source_sha KEY
    assert state.verdict == cei.STALE
    assert ce.TRUNCATION_SIGNATURE in state.summary


# ───────── 7. per-project one-shot (M-2 J-2 follow-up) ─────────
#
# The v1 marker was machine-level: the FIRST project's heal deleted it and
# every other exposed project kept its truncated rows with only
# --force-recreate as the escape.  The marker now holds a machine-level
# `observed` evidence block plus a per-project `healed` discharge map;
# owed(project) = observed ∧ project ∉ healed.


def test_two_exposed_projects_heal_independently(state_dir, tmp_path, monkeypatch):
    """THE per-project pin (red-proof target): machine-wide exposure → each
    exposed project owes ITS OWN one heal; the first project's discharge
    leaves every other project's owed state untouched, the second heals on
    its own next resync, and neither ever re-demotes."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    run_a, demote_a = _driver_world(
        monkeypatch, tmp_path, stale_sequence=[{"P_CodeFunction": 7}, {}],
    )
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("ProjA", tmp_path, analyzer)
    assert rc == 0
    assert demote_a == ["ProjA"], "the heal demotes exactly its own project"
    assert not ce.exposure_bump_owed("ProjA"), "ProjA discharged"
    assert ce.exposure_bump_owed("ProjB"), (
        "ProjB's owed state must survive ProjA's heal — the machine-level "
        "clear was the defect"
    )

    # ProjB heals independently on its own next resync.
    run_b, demote_b = _driver_world(
        monkeypatch, tmp_path, stale_sequence=[{"P_CodeFunction": 5}, {}],
    )
    rc = cr.run_resync_and_verify("ProjB", tmp_path, analyzer)
    assert rc == 0
    assert demote_b == ["ProjB"]
    assert len(run_b.calls) == 1
    assert not ce.exposure_bump_owed("ProjB")

    marker = json.loads((state_dir / ce.MARKER_REL).read_text(encoding="utf-8"))
    assert marker["observed"]["evidence"] == {"stale": {"kind": "live_probe"}}
    assert set(marker["healed"]) == {"ProjA", "ProjB"}

    # Third pass for ProjA: healed → never re-demotes (one-shot per project).
    _run_c, demote_c = _driver_world(
        monkeypatch, tmp_path, stale_sequence=[{}, {}],
    )
    rc = cr.run_resync_and_verify("ProjA", tmp_path, analyzer)
    assert rc == 0
    assert demote_c == [], "a healed project never re-demotes"


def test_healed_project_is_suppressed_at_detection(state_dir, tmp_path):
    """A discharged project short-circuits detection BEFORE any probe — even
    fresh truncating evidence cannot re-fire its one-shot — while an
    unhealed sibling's owed state is untouched."""
    assert ce.queue_exposure_bump({"stale": {"kind": "live_probe"}}) is True
    assert ce.clear_exposure_bump("ProjA") is True
    before = (state_dir / ce.MARKER_REL).read_bytes()

    status = ce.detect_and_queue(
        tmp_path, tmp_path, "ProjA",
        state=_TRUNCATING(), rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_ALREADY_QUEUED
    assert (state_dir / ce.MARKER_REL).read_bytes() == before
    assert ce.exposure_bump_owed("ProjB") is True


def test_legacy_v1_marker_migrates_to_per_project(state_dir, tmp_path, monkeypatch):
    """Backward-compatible read: a v1 machine-level marker (presence WAS the
    owed flag) migrates to observed-with-EMPTY-healed — conservatively, no
    project is credited as healed — and the first project's heal records
    ONLY its own entry, leaving every sibling owed."""
    legacy = {
        "queued_at": "2026-09-21T00:00:00Z",
        "reason": "v1 machine-level marker",
        "evidence": {"stale": {"kind": "prior_observation"}},
    }
    marker_path = state_dir / ce.MARKER_REL
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(json.dumps(legacy), encoding="utf-8")

    assert ce.exposure_bump_owed("ProjA") is True
    assert ce.exposure_bump_owed("ProjB") is True

    _run, demote_calls = _driver_world(
        monkeypatch, tmp_path, stale_sequence=[{"P_CodeFunction": 3}, {}],
    )
    analyzer = _analyzer_at(tmp_path)
    rc = cr.run_resync_and_verify("ProjA", tmp_path, analyzer)
    assert rc == 0
    assert demote_calls == ["ProjA"]
    assert not ce.exposure_bump_owed("ProjA")
    assert ce.exposure_bump_owed("ProjB"), (
        "one project's discharge under a migrated legacy marker must not "
        "suppress the projects that still owe"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["observed"]["queued_at"] == "2026-09-21T00:00:00Z"
    assert marker["observed"]["evidence"] == {
        "stale": {"kind": "prior_observation"},
    }
    assert list(marker["healed"]) == ["ProjA"]
    # Queueing over the migrated observation stays first-wins.
    assert ce.queue_exposure_bump({"stale": {"kind": "live_probe"}}) is False


def test_legacy_absent_marker_does_not_suppress_detection(state_dir, tmp_path):
    """A v1 marker already cleared machine-wide is ABSENT — the absence must
    not read as 'every project healed'.  Nothing is fabricated from it, and
    detection stays evidence-driven: a project that still owes re-records
    `observed` on the next update."""
    assert not (state_dir / ce.MARKER_REL).exists()
    assert ce.exposure_bump_owed("ProjB") is False  # nothing recorded (yet)
    status = ce.detect_and_queue(
        tmp_path, tmp_path, "ProjB",
        state=_TRUNCATING(), rows_probe=lambda name: True,
    )
    assert status == ce.STATUS_QUEUED
    assert ce.exposure_bump_owed("ProjB") is True


def test_clear_without_project_is_refused(state_dir):
    """The v1 whole-file unlink is gone: a no-arg clear would erase every
    other project's owed state AND the healed log — refused, marker intact.
    A vacuous clear (no marker at all) succeeds without fabricating a file."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    before = (state_dir / ce.MARKER_REL).read_bytes()
    assert ce.clear_exposure_bump() is False
    assert (state_dir / ce.MARKER_REL).read_bytes() == before

    (state_dir / ce.MARKER_REL).unlink()
    assert ce.clear_exposure_bump("ProjZ") is True
    assert not (state_dir / ce.MARKER_REL).exists()


def test_noarg_owed_is_the_conservative_legacy_read(state_dir):
    """`exposure_bump_owed()` with no project is the legacy machine-level
    approximation for not-yet-threaded consults: True while the observation
    exists and NOTHING has healed; once any project discharged it reports
    False rather than claiming a machine-wide owed state — the per-project
    truth stays available via the project argument."""
    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    assert ce.exposure_bump_owed() is True
    ce.clear_exposure_bump("ProjA")
    assert ce.exposure_bump_owed() is False
    assert ce.exposure_bump_owed("ProjB") is True


def test_queue_preserves_healed_entries(state_dir):
    """observed is first-wins and a queue write MERGES: the healed log is
    never clobbered, even in the odd shape where healed exists without an
    observation (a healed project stays suppressed; owed requires observed)."""
    marker_path = state_dir / ce.MARKER_REL
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps({"healed": {"ProjA": "2026-09-22T00:00:00Z"}}),
        encoding="utf-8",
    )
    assert ce.exposure_bump_owed("ProjA") is False  # healed, observed absent
    assert ce.exposure_bump_owed("ProjB") is False  # no observed → no owed
    assert ce.queue_exposure_bump({"stale": {"kind": "live_probe"}}) is True
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["healed"] == {"ProjA": "2026-09-22T00:00:00Z"}
    assert marker["observed"]["evidence"] == {"stale": {"kind": "live_probe"}}
    # Now observed exists: ProjA stays suppressed by its healed entry,
    # ProjB owes.
    assert ce.exposure_bump_owed("ProjA") is False
    assert ce.exposure_bump_owed("ProjB") is True


# ─────── 8. the marker's read-merge-write is serialized (MINOR-1) ───────


_HOLDER_SOURCE = """
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from vco_lib import code_embed_exposure as ce
from vco_lib.atomic import exclusive_file_lock

held = Path({held!r})
with exclusive_file_lock(ce._marker_lock_path()):
    # READ FIRST, write later — the ordinary read-merge-write of the other
    # driver. The window between them is where a second, UNLOCKED writer's
    # whole cycle fits (it takes ~1 ms), and its entry is then overwritten
    # from this stale snapshot. That is the real interleave, not a mock.
    marker = json.loads(ce._marker_path().read_text(encoding="utf-8"))
    held.write_text("held", encoding="utf-8")
    time.sleep({hold!r})
    marker.setdefault("healed", {{}})["ProjB"] = "2026-09-22T00:00:00Z"
    ce._write_json(ce._marker_path(), marker)
"""


def test_two_concurrent_discharges_cannot_lose_one(state_dir, tmp_path):
    """MINOR-1: `clear_exposure_bump` read-merge-writes the SHARED marker.

    `atomic_write_json` makes each WRITE atomic but not the cycle around it.
    Two detached resync drivers discharging DIFFERENT projects (the ordinary
    shape on a multi-project install: "Update all bundles", or a
    post-rebuild deferral-retry wave — the embed-admission semaphore's own
    default is 2) both read `healed={}`, both write, and the second erases
    the first project's discharge.  That project then re-heals: a second
    demote and a second full re-embed, which is precisely the "exactly once
    per project" promise this mechanism is made of.

    Driven with a REAL second process holding the lock, so the assertion is
    about the mechanism and not about a simulated snapshot: an unlocked
    `clear_exposure_bump` finishes inside the holder's window and loses,
    a locked one waits and merges.
    """
    import subprocess

    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    held = tmp_path / "held.flag"
    src = _HOLDER_SOURCE.format(repo=str(REPO_ROOT), held=str(held), hold=1.5)
    # child_env(), not a hand-rolled os.environ copy: this child imports
    # vco_lib and takes the SAME lock as the parent, so if it resolved a
    # different checkout (the documented site-packages shadow on this repo's
    # dev box) the two processes would contend on two different marker files
    # and the red-proof would pass while proving nothing. child_env puts the
    # repo root first on PYTHONPATH *and* pins VCT_ORCHESTRATOR_ROOT, which
    # PYTHONPATH alone cannot do. Pinned by
    # tests/test_v0292_fixround_child_env_lint.py.
    from tests.common.child_env import child_env

    holder = subprocess.Popen(  # noqa: S603 — our own argv, no shell
        [sys.executable, "-c", src], env=child_env(),
    )
    try:
        deadline = time.time() + 20
        while not held.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert held.exists(), "the holder process never acquired the lock"

        # THE call under test, made while the other process holds the lock.
        assert ce.clear_exposure_bump("ProjA") is True
    finally:
        holder.wait(timeout=30)

    healed = json.loads(
        (state_dir / ce.MARKER_REL).read_text(encoding="utf-8")
    )["healed"]
    assert set(healed) == {"ProjA", "ProjB"}, (
        "a concurrent discharge was LOST: the loser re-heals and pays a "
        "second full re-embed"
    )


def test_every_marker_writer_takes_the_lock(state_dir, monkeypatch):
    """The cheap structural pin beside the process-level one: each writer's
    read AND write happen inside one lock window.  Deleting the lock from any
    of them turns this red without waiting on a second process."""
    from vco_lib import atomic as _atomic

    events: list = []
    real_lock = _atomic.exclusive_file_lock

    @contextmanager
    def _recording(path):
        events.append(("acquire", str(path)))
        with real_lock(path):
            yield
        events.append(("release", str(path)))

    real_write = ce._write_json

    def _spy_write(path, payload):
        events.append(("write", str(path)))
        return real_write(path, payload)

    monkeypatch.setattr(_atomic, "exclusive_file_lock", _recording)
    monkeypatch.setattr(ce, "_write_json", _spy_write)

    def _writes_inside_a_lock(action) -> bool:
        events.clear()
        action()
        depth = 0
        inside = False
        for kind, _path in events:
            if kind == "acquire":
                depth += 1
            elif kind == "release":
                depth -= 1
            elif kind == "write":
                inside = inside or depth > 0
        return inside

    assert _writes_inside_a_lock(
        lambda: ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    ), "queue_exposure_bump writes outside the marker lock"
    assert _writes_inside_a_lock(
        lambda: ce.record_exposure_demote("ProjA")
    ), "record_exposure_demote writes outside the marker lock"
    assert _writes_inside_a_lock(
        lambda: ce.clear_exposure_bump("ProjA")
    ), "clear_exposure_bump writes outside the marker lock"


def test_spawn_gate_is_per_project(state_dir, tmp_path, monkeypatch):
    """The spawn owed-gate consults THIS project's entry: a healed project
    keeps the pre-M-2 not_owed short-circuit (zero extra embeds) while an
    unhealed sibling still spawns despite the positive zero stale count."""
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    _set_verdict(monkeypatch, "current")
    monkeypatch.setattr(
        cr, "count_stale_rows",
        lambda *a, **k: {
            "P_CodeModule": 0, "P_CodeClass": 0, "P_CodeFunction": 0,
        },
    )
    monkeypatch.setattr(cr, "count_cleanup_owed_rows", lambda *a, **k: 0)
    popen = _SpawnRecord()
    monkeypatch.setattr(cr.subprocess, "Popen", popen)
    _analyzer_at(tmp_path)

    ce.queue_exposure_bump({"stale": {"kind": "live_probe"}})
    assert ce.clear_exposure_bump("ProjA") is True

    result = cr.spawn_background_resync(
        tmp_path, "ProjA", python_exe=sys.executable,
    )
    assert result.status == "not_owed", "a healed project never re-spawns"
    assert popen.calls == []

    result = cr.spawn_background_resync(
        tmp_path, "ProjB", python_exe=sys.executable,
    )
    assert result.status == "launched"
    driver_calls = [c for c in popen.calls if "--run-resync" in c["argv"]]
    assert len(driver_calls) == 1
