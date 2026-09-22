# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.96 WP-3 (gate half): the codegraph resync refuses a STALE code-embed
service, on EVERY path.

Register issue 8 (2026-09-20): the update's resync driver pushed 3 156 embed
requests through a code-embed service whose own deferral flags it as silently
truncating over-window input at HTTP 200 — the ordering constraint "rebuild
the image first" existed only as prose. Review M-1 pins WHERE the gate must
live: INSIDE the driver (``run_resync_and_verify`` / the analyzer-spawn
boundary), because the deferral's auto-retry handler ``retry_codegraph_resync``
spawns ``--run-resync`` DIRECTLY, bypassing any install-time gate. Every path
— install trigger, auto-retry driver, manual ``--run-resync`` — must pass the
same verdict (``vco_lib.code_embed_image``: current / stale / unknown).

Pinned here:

* stale → NO analyzer spawn + a ``codegraph_embed_resync_pending`` ledger
  entry with rebuild-first ordering text, on BOTH the install-trigger path
  (``spawn_background_resync`` hands the entry back for install.py to record)
  AND the direct ``--run-resync`` driver path (the driver emits it itself —
  the M-1 pin; this is the test that catches the bypass);
* current → the walk proceeds;
* unknown → EXACTLY today's conservative self-degrade (walk proceeds, no
  stale entry written), including that a sourceless tree short-circuits to
  ``unknown`` WITHOUT an HTTP round-trip;
* retry budget: a stale verdict BLOCKS in the dispatcher BEFORE the STARTED
  row is written, so a persistently stale image burns none of the durable
  :data:`vco_lib.deferral_retry.MAX_ATTEMPTS` budget and the retry resumes
  by itself once the image turns current (the BLOCKED convention of WFT C7).

Hermeticity: the ONE verdict helper (``code_embed_image_verdict``) is
monkeypatched everywhere a verdict is needed, so no test contacts any real
service — doubly important on THIS machine, whose real code-embed service IS
the stale one.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_resync as cr  # noqa: E402
from vco_lib import deferral_retry as dr  # noqa: E402

CID = "codegraph_embed_resync_pending"


# ───────────────────────── shared fake world ─────────────────────────


class _RunRecord:
    """subprocess.run stand-in preserving the driver's historical seam."""

    def __init__(self, returncode=0):
        self.calls = []
        self._rc = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return types.SimpleNamespace(returncode=self._rc)


class _SpawnRecord:
    """Popen stand-in: records argv, never spawns anything."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return types.SimpleNamespace(pid=4321)


class _Runner:
    """deferral_retry runner seam: records argv, returns a fixed rc."""

    def __init__(self, rc=0):
        self.calls = []
        self._rc = rc

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        return self._rc


def _analyzer_at(tmp_path: Path) -> Path:
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    analyzer = scripts / "analyze_code_graph.py"
    analyzer.write_text("# stub analyzer\n", encoding="utf-8")
    return analyzer


def _ledger_md(repo_root: Path) -> str:
    path = Path(repo_root) / ".claude" / "context" / "UPDATE_DEFERRED.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _set_verdict(monkeypatch, verdict: str):
    """Pin the ONE verdict helper — the same one the driver, the spawn gate
    and the dispatcher's gate all consult."""
    monkeypatch.setattr(cr, "code_embed_image_verdict", lambda *a, **k: verdict)


def _quiet_driver_probes(monkeypatch, *, stale=None):
    """Silence the driver's non-gate machinery (Weaviate probes, sweep)."""
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: None)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: stale)


# ───────────────────── the ONE helper itself ─────────────────────


def _install_root_with_source(tmp_path: Path, *, sha: str = "d3adb33f") -> Path:
    """A tree that ``resolve_install_root`` accepts AND that carries the
    code-embed build context — i.e. an orchestrator clone, the only kind of
    tree that can say anything about the machine's image.

    ``image_source.py`` is a stub: ``checkout_source_sha`` loads that file BY
    PATH from the build context (the same file the Dockerfiles COPY), so a
    two-line stub gives a deterministic expected digest with no dependency on
    the real service source.
    """
    root = tmp_path / "install-root"
    (root / "vco_lib").mkdir(parents=True)
    (root / ".claude").mkdir(parents=True)
    source = root / "claude_mcp_servers" / "code_embedding_service"
    source.mkdir(parents=True)
    (source / "image_source.py").write_text(
        f"def source_sha(source_dir):\n    return {sha!r}\n", encoding="utf-8",
    )
    return root


def test_sourceless_project_is_judged_against_the_INSTALL_root(
    monkeypatch, tmp_path,
):
    """SHIP-GATE MAJOR-2 (2026-09-22). A per-project tree bundles no service
    source, so judging the verdict against IT could only ever answer
    ``unknown`` — and ``unknown`` never gates. Every per-project resync
    therefore embedded through a stale image exactly as before WP-3 existed:
    the register's issue 8 reproduced on the bundle path, with the protection
    live only for the orchestrator ROOT (whose tree does carry the source).

    The machine has exactly ONE code-embed service, built from the INSTALL
    root's source — so that is the tree the verdict must be judged against,
    the same probe `_maybe_run_exposure_heal` already makes.
    """
    from vco_lib import code_embed_image as cei

    monkeypatch.delenv("VCT_CODE_EMBED_BUILD_CONTEXT", raising=False)
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(_install_root_with_source(tmp_path)))
    # The machine's ONE service is a pre-v0.2.92 image: /health answers, and
    # its payload has no `source_sha` KEY at all (the truncating cohort).
    monkeypatch.setattr(cei, "probe_health", lambda *a, **k: {"status": "ok"})
    project = tmp_path / "a-user-project"
    project.mkdir()

    assert cr.code_embed_image_verdict(project) == "stale"


def test_sourceless_project_with_a_current_machine_image_proceeds(
    monkeypatch, tmp_path,
):
    """The leave-alone half of the same decision: the install-root probe must
    not turn into a blanket refusal. A machine whose image matches the source
    it was built from is ``current`` — the walk proceeds."""
    from vco_lib import code_embed_image as cei

    monkeypatch.delenv("VCT_CODE_EMBED_BUILD_CONTEXT", raising=False)
    monkeypatch.setenv(
        "VCT_INSTALL_ROOT", str(_install_root_with_source(tmp_path, sha="beef01")),
    )
    monkeypatch.setattr(
        cei, "probe_health",
        lambda *a, **k: {"status": "ok", "source_sha": "beef01"},
    )
    project = tmp_path / "a-user-project"
    project.mkdir()

    assert cr.code_embed_image_verdict(project) == "current"


def _no_source_anywhere(monkeypatch):
    """No build context in the project tree, and no install root carrying
    one either — the site-packages-shadow shape (vco_lib importable from
    outside any clone)."""
    from vco_lib import python_exe

    monkeypatch.delenv("VCT_CODE_EMBED_BUILD_CONTEXT", raising=False)
    monkeypatch.setattr(python_exe, "resolve_install_root", lambda *a, **k: None)


def test_no_source_anywhere_still_catches_a_pre_v0292_image(
    monkeypatch, tmp_path,
):
    """WP-3 judgment call 3, resolved (owner directive 2026-09-22).

    With no build context anywhere there is no digest to COMPARE — but the
    absence of the ``source_sha`` KEY in ``/health`` is positive, digest-free
    evidence of a pre-v0.2.92 image, and that is precisely the population
    that truncates silently. The gate must bite on it."""
    from vco_lib import code_embed_image as cei

    _no_source_anywhere(monkeypatch)
    monkeypatch.setattr(cei, "probe_health", lambda *a, **k: {"status": "ok"})

    assert cr.code_embed_image_verdict(tmp_path) == "stale"


def test_no_source_anywhere_and_a_post_v0292_image_is_unknown(
    monkeypatch, tmp_path,
):
    """Leave-alone: an image that REPORTS a digest carries v0.2.92's refusal,
    so with nothing to compare against "cannot say" is the honest answer —
    and ``unknown`` never gates."""
    from vco_lib import code_embed_image as cei

    _no_source_anywhere(monkeypatch)
    monkeypatch.setattr(
        cei, "probe_health", lambda *a, **k: {"status": "ok", "source_sha": "ab"},
    )

    assert cr.code_embed_image_verdict(tmp_path) == "unknown"


def test_no_source_anywhere_and_no_service_is_unknown(monkeypatch, tmp_path):
    """And with no payload at all there is no evidence of anything."""
    from vco_lib import code_embed_image as cei

    _no_source_anywhere(monkeypatch)
    monkeypatch.setattr(cei, "probe_health", lambda *a, **k: None)

    assert cr.code_embed_image_verdict(tmp_path) == "unknown"


def test_a_named_tree_is_required_and_None_never_probes(monkeypatch, tmp_path):
    """``repo_root=None`` is a caller that named no tree at all: still
    ``unknown``, and still without opening a socket."""
    from vco_lib import code_embed_image as cei

    _no_source_anywhere(monkeypatch)

    def _fail(*a, **k):  # pragma: no cover — the assertion IS the test
        raise AssertionError("probe_health must not run: no tree was named")

    monkeypatch.setattr(cei, "probe_health", _fail)
    assert cr.code_embed_image_verdict(None) == "unknown"


def test_a_pruned_install_root_falls_back_to_the_payload_evidence(
    monkeypatch, tmp_path,
):
    """An install root that resolves but carries no build context (a partial
    clone) is the same "no digest" case — decided from the payload alone."""
    from vco_lib import code_embed_image as cei

    sourceless_root = tmp_path / "pruned-root"
    (sourceless_root / "vco_lib").mkdir(parents=True)
    (sourceless_root / ".claude").mkdir(parents=True)
    monkeypatch.delenv("VCT_CODE_EMBED_BUILD_CONTEXT", raising=False)
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(sourceless_root))
    monkeypatch.setattr(cei, "probe_health", lambda *a, **k: {"status": "ok"})

    assert cr.code_embed_image_verdict(tmp_path / "proj") == "stale"


# ─────── the gates reach the install root too (ship-gate MAJOR-2) ───────
#
# The three enforcement points all pass the PROJECT as their root. Pinning the
# helper alone would not have caught the defect: the helper answered
# "unknown" correctly for the tree it was given — the bug was that nobody
# asked about the tree that owns the service. These drive the REAL helper (no
# `_set_verdict`) through each gate with a sourceless project folder.


def _stale_machine(monkeypatch, tmp_path):
    """A machine whose ONE code-embed service runs a pre-v0.2.92 image, seen
    from a per-project tree that bundles no service source."""
    from vco_lib import code_embed_image as cei

    monkeypatch.delenv("VCT_CODE_EMBED_BUILD_CONTEXT", raising=False)
    monkeypatch.setenv("VCT_INSTALL_ROOT", str(_install_root_with_source(tmp_path)))
    monkeypatch.setattr(cei, "probe_health", lambda *a, **k: {"status": "ok"})
    project = tmp_path / "a-user-project"
    project.mkdir()
    return project


def test_driver_refuses_for_a_sourceless_project_on_a_stale_machine(
    monkeypatch, tmp_path,
):
    """MAJOR-2 at the DRIVER gate — the one no caller can bypass."""
    project = _stale_machine(monkeypatch, tmp_path)
    _quiet_driver_probes(monkeypatch, stale={"CodeFunction": 3})
    run = _RunRecord(returncode=0)
    monkeypatch.setattr(cr.subprocess, "run", run)
    analyzer = _analyzer_at(project)

    rc = cr.run_resync_and_verify("MyProj", project, analyzer)

    assert rc == 0
    assert run.calls == [], (
        "a per-project resync must NOT embed through the machine's stale image"
    )
    assert f"## {CID}" in _ledger_md(project)


def test_spawn_refuses_for_a_sourceless_project_on_a_stale_machine(
    monkeypatch, tmp_path,
):
    """MAJOR-2 at the install-trigger gate (defense in depth around the
    driver's)."""
    project = _stale_machine(monkeypatch, tmp_path)
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    popen = _SpawnRecord()
    monkeypatch.setattr(cr.subprocess, "Popen", popen)
    _analyzer_at(project)

    result = cr.spawn_background_resync(
        project, "MyProj", python_exe=sys.executable, check_owed=False,
    )

    assert result.status == "deferred"
    assert popen.calls == [], "no child of any kind may be spawned on stale"


def test_dispatcher_blocks_for_a_sourceless_project_on_a_stale_machine(
    monkeypatch, tmp_path,
):
    """MAJOR-2 at the deferral-retry gate: this dispatcher is what ran the
    2026-09-20 walk that embedded 3 156 requests through the stale image, and
    the folder it passes is ALWAYS a project folder."""
    project = _stale_machine(monkeypatch, tmp_path)
    runner = _Runner(rc=0)
    monkeypatch.setattr(dr, "_project_name", lambda folder: "MyProj")
    _analyzer_at(project)

    results = dr.dispatch(
        project, condition_ids=[CID],
        backend_probe=lambda folder, kind: True, runner=runner,
    )

    assert [r.status for r in results] == [dr.SKIPPED]
    assert "stale" in results[0].detail.lower()
    assert runner.calls == [], "no handler child may run while stale"
    assert dr.attempt_count(project, CID) == 0, "BLOCKED rows must not count"


# ───────────────────── driver path (--run-resync) ─────────────────────


def test_driver_stale_refuses_walk_and_defers(monkeypatch, tmp_path):
    """THE M-1 PIN. The deferral's auto-retry spawns `--run-resync` DIRECTLY
    (retry_codegraph_resync → run_resync_and_verify), so a gate that lived
    only on the install-time spawn would be bypassed here. Stale verdict →
    the analyzer subprocess is NOT run, and the driver itself writes the
    ledger entry with rebuild-first ordering text."""
    _set_verdict(monkeypatch, "stale")
    _quiet_driver_probes(monkeypatch, stale={"CodeFunction": 12})
    run = _RunRecord(returncode=0)
    monkeypatch.setattr(cr.subprocess, "run", run)
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)

    assert rc == 0, "refusal is a deferral, not a driver failure"
    assert run.calls == [], "stale image: the analyzer must NOT be spawned"
    md = _ledger_md(tmp_path)
    assert f"## {CID}" in md, "the stale-image entry reuses the SAME condition id"
    assert "stale" in md.lower()
    # Ordering text is the load-bearing part: rebuild FIRST, resync SECOND.
    assert "install.py --update" in md
    assert md.index("install.py --update") < md.index("--run-resync"), (
        "the entry must say rebuild the image BEFORE re-running the resync"
    )


def test_driver_current_proceeds(monkeypatch, tmp_path):
    """Current verdict → the walk proceeds exactly as before the gate."""
    _set_verdict(monkeypatch, "current")
    _quiet_driver_probes(monkeypatch, stale={})
    run = _RunRecord(returncode=0)
    monkeypatch.setattr(cr.subprocess, "run", run)
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)

    assert rc == 0
    assert len(run.calls) == 1, "the analyzer spawn is the walk"
    argv = run.calls[0]["argv"]
    assert str(analyzer) in argv and "--project" in argv


def test_driver_unknown_keeps_today_behavior(monkeypatch, tmp_path):
    """Unknown verdict → today's conservative self-degrade, unchanged: the
    walk runs, and no stale-image entry is written (nothing was refused)."""
    _set_verdict(monkeypatch, "unknown")
    _quiet_driver_probes(monkeypatch, stale={})
    run = _RunRecord(returncode=0)
    monkeypatch.setattr(cr.subprocess, "run", run)
    analyzer = _analyzer_at(tmp_path)

    rc = cr.run_resync_and_verify("MyProj", tmp_path, analyzer)

    assert rc == 0
    assert len(run.calls) == 1, "unknown must NOT gate the walk"
    assert _ledger_md(tmp_path) == ""


# ───────────────────── install-trigger path (spawn) ─────────────────────


def _spawn_world(monkeypatch, tmp_path):
    """Common spawn fixtures: kill-switch off, service live, no owed probe."""
    monkeypatch.delenv("VCT_RESYNC_SPAWN_DISABLED", raising=False)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    analyzer = _analyzer_at(tmp_path)
    popen = _SpawnRecord()
    monkeypatch.setattr(cr.subprocess, "Popen", popen)
    return analyzer, popen


def test_spawn_stale_defers_with_entry_no_popen(monkeypatch, tmp_path):
    """Install-trigger path: stale verdict → status deferred, the SAME
    condition id handed back for install.py to record (its existing
    `deferred` arm calls `deferral_report.add_entry(result.deferral)`),
    and no Popen at all — not even the driver child."""
    analyzer, popen = _spawn_world(monkeypatch, tmp_path)
    _set_verdict(monkeypatch, "stale")

    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe=sys.executable, check_owed=False,
    )

    assert result.status == "deferred"
    assert result.pid is None
    assert popen.calls == [], "no child of any kind may be spawned on stale"
    # ResyncTriggerResult.deferral is Optional[object] by design (the module
    # must stay importable without deferral_report) — read via getattr.
    entry = result.deferral
    assert entry is not None
    assert getattr(entry, "condition_id", None) == CID, "no new condition id"
    assert "stale" in str(getattr(entry, "title", "")).lower()
    cmd = str(getattr(entry, "command_to_apply", ""))
    assert "install.py --update" in cmd and "--run-resync" in cmd
    assert cmd.index("install.py --update") < cmd.index("--run-resync")


def test_spawn_current_launches(monkeypatch, tmp_path):
    analyzer, popen = _spawn_world(monkeypatch, tmp_path)
    _set_verdict(monkeypatch, "current")

    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe=sys.executable, check_owed=False,
    )

    assert result.status == "launched"
    assert result.pid == 4321
    assert popen.calls, "current verdict: the detached driver must be spawned"
    # The rider children (prune/backfill/summary) fire first; the DRIVER is
    # the --run-resync child — the only one whose walk embeds.
    driver_calls = [c for c in popen.calls if "--run-resync" in c["argv"]]
    assert len(driver_calls) == 1
    argv = driver_calls[0]["argv"]
    assert "-m" in argv and "vco_lib.codegraph_resync" in argv


def test_spawn_unknown_launches(monkeypatch, tmp_path):
    """Unknown verdict on the install path → today's behavior: launch."""
    analyzer, popen = _spawn_world(monkeypatch, tmp_path)
    _set_verdict(monkeypatch, "unknown")

    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe=sys.executable, check_owed=False,
    )

    assert result.status == "launched"
    assert popen.calls


# ───────────────────── retry budget (dispatcher gate) ─────────────────────


def test_dispatch_stale_blocks_without_burning_the_cap(monkeypatch, tmp_path):
    """A stale image must not consume the durable retry budget. Pre-fix the
    backend gate passed (the stale service IS reachable), the driver refused,
    came back INCONCLUSIVE with the STARTED row already written — three
    passes and the cap retired the retry forever, including for the day the
    image is finally rebuilt. The stale verdict now BLOCKS before STARTED
    (the WFT C7 convention), so the retry resumes by itself once current."""
    from unittest import mock

    runner = _Runner(rc=0)
    # The suite pins the hub resolver off (VCT_DISABLE_HUB_RESOLVER), so the
    # real _project_name falls back to env and finds nothing for a tmp folder;
    # the retry handler then legitimately SKIPS. Name the project explicitly —
    # the runner is faked, the handler's argv is what matters here.
    monkeypatch.setattr(dr, "_project_name", lambda folder: "MyProj")
    _analyzer_at(tmp_path)  # the real handler resolves it before running
    backend_up = lambda folder, kind: True  # noqa: E731 — readable inline

    # 1) many passes while the image is stale: nothing starts, nothing burns.
    for _ in range(5):
        _set_verdict(monkeypatch, "stale")
        results = dr.dispatch(
            tmp_path, condition_ids=[CID],
            backend_probe=backend_up, runner=runner,
        )
        assert [r.status for r in results] == [dr.SKIPPED]
        assert "stale" in results[0].detail.lower()
    assert runner.calls == [], "no handler child may run while stale"
    assert dr.attempt_count(tmp_path, CID) == 0, "BLOCKED rows must not count"
    history = dr.retry_history(tmp_path, CID)
    assert history.blocked == 5 and history.attempts == 0

    # 2) the image turns current: the retry fires on the very next pass —
    #    exactly the `test_skips_do_not_burn_the_cap` contract.
    _set_verdict(monkeypatch, "current")
    with mock.patch.object(dr, "_record_resolution"):
        results = dr.dispatch(
            tmp_path, condition_ids=[CID],
            backend_probe=backend_up, runner=runner,
        )
    assert [r.status for r in results] == [dr.RETRIED]
    assert len(runner.calls) == 1
    assert dr.attempt_count(tmp_path, CID) == 1
    assert "--run-resync" in " ".join(runner.calls[0])


def test_dispatch_gate_consults_the_one_helper(monkeypatch, tmp_path):
    """The dispatcher's gate must call the SAME verdict helper the driver
    does (review M-1: a verdict only one surface consults is a gate the
    other surface bypasses). Patching the helper in codegraph_resync must
    be sufficient to steer the dispatcher."""
    runner = _Runner(rc=0)
    seen = {}

    def _spy(repo_root, code_embed_url=None):
        seen["repo_root"] = str(repo_root)
        return "stale"

    monkeypatch.setattr(cr, "code_embed_image_verdict", _spy)
    results = dr.dispatch(
        tmp_path, condition_ids=[CID],
        backend_probe=lambda folder, kind: True, runner=runner,
    )
    assert [r.status for r in results] == [dr.SKIPPED]
    assert seen.get("repo_root") == str(tmp_path), (
        "the dispatcher must ask about ITS folder through the ONE helper"
    )
    assert runner.calls == []


def test_walk_handler_also_blocks_on_stale(monkeypatch, tmp_path):
    """WP-3 review MAJOR-1: the `code_graph_walk` retry handler does a BARE
    analyzer walk that never enters the resync driver — the driver-side
    verdict gate cannot see it. Without the needs_current_code_embed_image
    flag on THAT handler, a stale-but-UP service passes the reachability-
    only backend gate and this retry embeds straight through the stale
    image: the exact defect WP-3 exists to close, one registry row away."""
    from vco_lib import deferral_retry as dr

    runner = _Runner(rc=0)
    monkeypatch.setattr(dr, "_project_name", lambda folder: "MyProj")
    _analyzer_at(tmp_path)
    _set_verdict(monkeypatch, "stale")
    walk_cids = [
        "code_graph_no_embedding_backend",
        "code_graph_code_backend_unreachable",
    ]
    results = dr.dispatch(
        tmp_path, condition_ids=walk_cids,
        backend_probe=lambda folder, kind: True, runner=runner,
    )
    assert [r.status for r in results] [0] == dr.SKIPPED, (
        "the walk handler must BLOCK on a stale image verdict, not pass "
        "through the reachability-only backend gate"
    )
    assert runner.calls == [], "no analyzer child may run while stale"
    for cid in walk_cids:
        assert dr.attempt_count(tmp_path, cid) == 0, (
            "BLOCKED rows must not burn the retry budget (WFT C7)"
        )
