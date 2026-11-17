# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 — the chunker re-sync entry: stop the KG re-embed loop, record the
code-graph half where it is read, and retire entries already stuck.

Three defects, one ledger entry (``chunker_preset_overhaul_pending``):

1. ``sync_knowledge_graph.py`` kept its per-node gate armed for as long as the
   ENTRY lived — i.e. until the code-graph half was recorded too — so every
   ``--all`` in between re-embedded the same nodes (the split-rows re-lay keeps
   no per-row record). The gate now also asks whether the KG half is recorded
   at the current revision (``chunker_revision.kg_half_recorded``).
2. The analyzer recorded the code-graph half in the ledger root
   (``$VCT_ORCHESTRATOR_ROOT`` in every session-spawned walk) instead of the
   project it walked, so no project but the orchestrator root could clear.
3. Projects already stuck that way are healed without a new walk, on positive
   evidence only (``chunker_revision.completed_walk_evidence``).

Nothing here reaches a service: the sync runs against an in-memory store with
a counting embedder, and the analyzer children run with stubbed walks.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests import test_wiring_w8_shared_chunk_plan as W8  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402
from vco_lib import chunker_revision as cr  # noqa: E402
from vco_lib import codegraph_deferrals as cd  # noqa: E402
from vco_lib import codegraph_extractor_generation as ceg  # noqa: E402
from vco_lib import deferral_retry  # noqa: E402
from vco_lib import doctor  # noqa: E402
from vco_lib.deferral_emit import emit  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402

CID = "chunker_preset_overhaul_pending"
ANALYZER = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
ARCTIC = "snowflake-arctic-embed2:latest"
QWEN3 = "qwen3-embedding:0.6b"


def _seed_entry(folder: Path) -> None:
    emit(folder, DeferralEntry(
        condition_id=CID,
        title="KG + codegraph re-sync recommended (chunker revision changed)",
        detected="test fixture: a chunker revision crossing is pending repair",
        why_deferred="test fixture",
        command_to_apply=".claude/scripts/kg-sync --all",
        severity="info",
    ))


def _cids(folder: Path) -> list:
    return [e.condition_id for e in DeferralReport.read(folder).entries]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _crossed(folder: Path, at: str = "2026-11-13T12:45:19Z", revision=None) -> None:
    """The gate's sentinel: this project crossed to ``revision`` at ``at``."""
    _write_json(cr.state_path(folder), {
        "revision": revision or cr.current_revision(), "updated_at": at,
    })


def _walked(folder: Path, at: str = "2026-11-13T12:58:40Z", *,
            note: str = "force-rewalk completed", generation=None) -> None:
    """The analyzer's own completion stamp for a whole-repo forced walk."""
    _write_json(ceg.stamp_path(folder), {
        "generation": generation or ceg.CURRENT_EXTRACTOR_GENERATION,
        "note": note,
        "written_at": at,
        "written_by": "vco_lib.codegraph_extractor_generation",
    })


# ── 1. the sync gate ────────────────────────────────────────────────────────


@pytest.fixture
def kg(tmp_path, monkeypatch):
    """A project folder + the W8 sync harness' env handling."""
    from vco_lib import kg_embed_model

    case = W8.W8SharedChunkPlanTests(
        "test_node_between_the_two_gates_is_single_chunk_on_both")
    case.root = tmp_path
    case._env = {k: os.environ.get(k) for k in W8._ENV_KEYS}
    # `vco_lib.kg_embed_model` is ONE module shared by every loaded copy of
    # the script: patch it through monkeypatch so nothing outlives the test.
    monkeypatch.setattr(kg_embed_model, "resolve_kg_model",
                        lambda *a, **k: types.SimpleNamespace(carry=None))
    monkeypatch.setattr(kg_embed_model, "fill_before_walk", lambda *a, **k: None)
    monkeypatch.setattr(kg_embed_model, "after_walk", lambda *a, **k: None)
    yield case
    case._restore()


class _CountingServer(W8._SyncServer):
    """Writes qwen3 + arctic vectors; counts every embed call."""

    def __init__(self, client=None):
        super().__init__()
        self.embedded: list = []
        self.embedding_service = types.SimpleNamespace(
            text_model_id=QWEN3,
            configured_text_write_models=lambda: {
                "qwen3_embed": QWEN3, "arctic2_embed": ARCTIC,
            },
        )
        if client is not None:
            self.client = client

    def _get_all_kg_embeddings_tagged(self, text):
        self.embedded.append(text)
        return {self.text_vector_slot: [0.1, 0.2], "arctic2_embed": [0.3, 0.4]}, []


#: Long enough that the arctic secondary MAY have refused it (the split-rows
#: heal's arm 2 fires for it during a crossing), short enough for one chunk.
_MAYBE_AVERAGED = "lorem ipsum dolor sit amet. " * 300
_FITS = "short prose. " * 20


def _load(case):
    return W8._load_sync_module(case.root)


def _seed_tree(case) -> "tuple[Path, Path]":
    """Two nodes already stored at their current content (pre-crossing rows)."""
    big = case._write_node("knowledge/concepts/maybe_avg.md", "Maybe Avg", _MAYBE_AVERAGED)
    small = case._write_node("knowledge/concepts/fits.md", "Fits", _FITS)
    return big, small


def _sync(mod, server, path) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert bool(mod.sync_node(server, path)), buf.getvalue()[-1500:]
    return buf.getvalue()


def _stored(case):
    """A store holding both nodes, written BEFORE the crossing was detected."""
    big, small = _seed_tree(case)
    mod = _load(case)
    server = _CountingServer()
    _sync(mod, server, big)
    _sync(mod, server, small)
    return big, small, server.client


class TestSyncGate:
    def test_entry_without_the_kg_stamp_still_re_lays(self, kg):
        """Leave-alone: the pending state proper — the half has not run."""
        big, small, client = _stored(kg)
        _seed_entry(kg.root)
        mod, server = _load(kg), _CountingServer(client)
        assert mod._chunker_resync_pending() is True
        out = _sync(mod, server, big) + _sync(mod, server, small)
        assert len(server.embedded) == 1, out[-1200:]
        assert "may be an average of split pieces" in out

    def test_entry_with_the_kg_stamp_at_the_current_revision_hash_skips(self, kg):
        """The defect: the entry outlives the KG half (it waits for the code
        graph), and every pass in between re-embedded the same nodes."""
        big, small, client = _stored(kg)
        _seed_entry(kg.root)
        assert cr.record_resync_half(kg.root, "kg") is True
        mod, server = _load(kg), _CountingServer(client)
        assert mod._chunker_resync_pending() is False
        out = _sync(mod, server, big) + _sync(mod, server, small)
        assert server.embedded == [], out[-1200:]
        assert out.count("Embed-skip") == 2
        assert CID in _cids(kg.root), "the sync never retires the entry itself"

    def test_a_stamp_from_an_older_revision_does_not_disarm(self, kg):
        _seed_entry(kg.root)
        assert cr.record_resync_half(kg.root, "kg", "v0.0.1-older") is True
        assert _load(kg)._chunker_resync_pending() is True

    def test_the_other_half_alone_does_not_disarm(self, kg):
        _seed_entry(kg.root)
        assert cr.record_resync_half(kg.root, "codegraph") is True
        assert _load(kg)._chunker_resync_pending() is True

    def test_an_unreadable_stamp_keeps_the_gate_armed(self, kg):
        _seed_entry(kg.root)
        path = cr.resync_state_path(kg.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        assert _load(kg)._chunker_resync_pending() is True

    def test_an_unreadable_revision_keeps_the_gate_armed(self, kg):
        _seed_entry(kg.root)
        cr.record_resync_half(kg.root, "kg")
        mod = _load(kg)
        with mock.patch.object(cr, "current_revision", return_value=None):
            assert mod._chunker_resync_pending() is True

    def test_rechunk_still_arms_the_comparison_over_a_recorded_half(self, kg):
        _seed_entry(kg.root)
        cr.record_resync_half(kg.root, "kg")
        mod = _load(kg)
        mod._RECHUNK_FORCED = True
        assert mod._chunker_resync_pending() is True

    def test_no_entry_is_not_pending_whatever_the_stamp_says(self, kg):
        assert _load(kg)._chunker_resync_pending() is False


def _run_all(case, client):
    """One ``--all`` pass through the script's own ``main()``.

    A fresh module per pass (the gate's answer is cached per process, exactly
    as two real runs would see it). The walk, the embed-skip gate and every
    post-walk clear/stamp are the shipped code; only the service clients and
    the legs that need a live backend are doubles. Returns
    ``(exit code, files re-embedded, output)``.
    """
    mod = _load(case)
    server = _CountingServer(client)
    mod.EmbeddingService = types.SimpleNamespace(
        for_project=lambda *a, **k: types.SimpleNamespace(close=lambda: None))
    mod.WeaviateMCPServer = lambda **kw: server
    mod.ensure_collection_exists = lambda s: True
    mod.ensure_dev_collection_exists = lambda s: True
    mod._refuse_fixture_shaped_targets = lambda: None
    mod._fill_collections = lambda: []
    mod._prune_stored_only_rows = lambda **k: None
    mod._regen_node_formats_after_full_sync = lambda: None
    mod._record_context_triple = lambda *a, **k: None
    mod._record_metadata_repair_pass = lambda *a, **k: None
    mod.sync_all_docs = lambda s: mod.SyncTally()
    re_embedded: list = []
    real_sync_node = mod.sync_node

    def _counting_sync_node(s, path):
        before = len(s.embedded)
        outcome = real_sync_node(s, path)
        if len(s.embedded) > before:
            re_embedded.append(Path(path).name)
        return outcome

    mod.sync_node = _counting_sync_node  # the real `sync_all_nodes` calls it
    buf = io.StringIO()
    code = None
    with mock.patch.object(sys, "argv", ["sync_knowledge_graph.py", "--all"]):
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                mod.main()
            except SystemExit as exc:
                code = exc.code
    return code, re_embedded, buf.getvalue()


class TestTwoConsecutivePasses:
    """THE measurement: files re-embedded by pass 2 of two ``--all`` passes on
    a tree whose entry is pending (before the fix: the same as pass 1)."""

    def test_pass_two_re_embeds_nothing(self, kg):
        # 12 files: 7 whose arctic vector may be an average, 5 that provably fit.
        paths = [
            kg._write_node(f"knowledge/concepts/maybe_avg_{i}.md", f"Maybe Avg {i}",
                           f"node {i}. " + _MAYBE_AVERAGED)
            for i in range(7)
        ] + [
            kg._write_node(f"knowledge/concepts/fits_{i}.md", f"Fits {i}",
                           f"node {i}. " + _FITS)
            for i in range(5)
        ]
        mod, seeded = _load(kg), _CountingServer()
        for path in paths:
            _sync(mod, seeded, path)
        client = seeded.client
        _seed_entry(kg.root)

        code1, first, out1 = _run_all(kg, client)
        assert code1 == 0, out1[-2000:]
        assert first == [f"maybe_avg_{i}.md" for i in range(7)], out1[-2000:]
        assert cr.read_resync_stamps(kg.root) == {"kg": cr.current_revision()}
        assert CID in _cids(kg.root), "the code-graph half is still owed"

        code2, second, out2 = _run_all(kg, client)
        assert code2 == 0, out2[-2000:]
        print(f"MEASUREMENT files={len(paths)} re-embedded: "
              f"pass1={len(first)} pass2={len(second)}")
        assert second == [], (
            f"pass 2 re-embedded {len(second)} file(s) {second} — the pending "
            f"entry must not re-lay nodes once the KG half is recorded"
        )

    def test_a_pass_with_a_failure_records_no_half_and_the_next_pass_retries(self, kg):
        """Leave-alone: only a CLEAN pass is the KG half."""
        _big, _small, client = _stored(kg)
        _seed_entry(kg.root)
        bad = kg.root / "knowledge" / "concepts" / "broken.md"
        bad.write_bytes(b"\xff\xfe not utf-8 \xff")
        code, _first, out = _run_all(kg, client)
        if code == 0:
            pytest.skip("this fixture did not make the walk fail")
        assert cr.read_resync_stamps(kg.root) == {}, out[-1500:]
        assert _load(kg)._chunker_resync_pending() is True


# ── 2. the code-graph half is recorded in the WALKED project ────────────────


class TestRecordSuccessfulWalk:
    def test_the_half_goes_to_the_walked_project_not_the_ledger_root(self, tmp_path):
        root, project = tmp_path / "orchestrator", tmp_path / "project"
        root.mkdir(), project.mkdir()
        cd.record_successful_walk(root, False, True, project)
        assert cr.read_resync_stamps(project) == {"codegraph": cr.current_revision()}
        assert cr.read_resync_stamps(root) == {}

    def test_a_walk_of_an_extra_code_folder_records_nothing(self, tmp_path):
        root = tmp_path / "orchestrator"
        root.mkdir()
        cd.record_successful_walk(root, True, True, None)
        assert cr.read_resync_stamps(root) == {}

    def test_a_plain_walk_records_nothing_in_either_tree(self, tmp_path):
        root, project = tmp_path / "orchestrator", tmp_path / "project"
        root.mkdir(), project.mkdir()
        cd.record_successful_walk(root, False, False, project)
        assert cr.read_resync_stamps(project) == {}
        assert cr.read_resync_stamps(root) == {}

    def test_a_caller_that_names_no_walked_root_keeps_the_ledger_root(self, tmp_path):
        cd.record_successful_walk(tmp_path, True)
        assert cr.read_resync_stamps(tmp_path) == {"codegraph": cr.current_revision()}

    def test_the_backend_clear_still_targets_the_ledger_root(self, tmp_path):
        """MAJOR-A unchanged: emit-root == clear-root."""
        root, project = tmp_path / "orchestrator", tmp_path / "project"
        root.mkdir(), project.mkdir()
        cd.emit_no_backend(root, RuntimeError("down"))
        cd.emit_no_backend(project, RuntimeError("down"))
        assert cd.record_successful_walk(root, False, True, project) == 1
        assert _cids(root) == []
        assert _cids(project) == ["code_graph_no_embedding_backend"]


def _forced_walk(tmp_path: Path, repo: Path, orchestrator_root: Path, *argv: str):
    """``analyze_code_graph.py main()`` in a child, walk stubbed, exit path real."""
    runner = tmp_path / f"runner_{len(list(tmp_path.glob('runner_*.py')))}.py"
    runner.write_text(textwrap.dedent(f"""\
        import sys, importlib.util
        spec = importlib.util.spec_from_file_location("acg", r"{ANALYZER}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        C = mod.CodeGraphAnalyzer
        C.connect = lambda self: (setattr(self, "client", object()) or True)
        C.close = lambda self: None
        C.create_collections = lambda self, force=False, *, repo_path=None: None
        C.create_cross_references = lambda self, changed_files=None: {{"calls": 0, "extends": 0, "imports": 0}}
        C.analyze_repository = lambda self, *a, **kw: {{
            "files_analyzed": 1, "files_skipped": 0, "modules": 1, "classes": 0,
            "functions": 1, "apis": 0, "insert_errors": 0}}
        class _Svc:
            code_vector_slot = "codesage_embed"; code_model_id = "m"; code_dim = 8
            text_vector_slot = "qwen3_embed"; text_model_id = "t"; text_dim = 8
            def code_backend_ready(self): return True
            def text_backend_ready(self): return True
            def close(self): return None
        mod.EmbeddingService.for_project = classmethod(lambda cls, *a, **kw: _Svc())
        sys.argv = ["analyze_code_graph.py"] + sys.argv[1:]
        sys.exit(mod.main())
    """))
    return subprocess.run(
        [sys.executable, str(runner), str(repo), "--project", "Acme", *argv],
        capture_output=True, text=True, timeout=120,
        # The session's shape: $VCT_ORCHESTRATOR_ROOT names the orchestrator
        # root, NOT the walked project. PYTHONPATH (child_env) still resolves
        # vco_lib from this checkout.
        env=child_env(VCT_ORCHESTRATOR_ROOT=str(orchestrator_root),
                      VCT_JOERN_AVAILABLE="0"),
    )


def _project(tmp_path: Path, name: str = "project") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return repo


class TestForcedWalkRealOrder:
    """A forced walk on a NON-root project, with the session's env."""

    def test_the_entry_clears_on_the_walked_project(self, tmp_path):
        orchestrator = tmp_path / "orchestrator"
        orchestrator.mkdir()
        project = _project(tmp_path)
        _seed_entry(project)
        cr.record_resync_half(project, "kg")
        assert cr.resync_still_owed(project) is True

        proc = _forced_walk(tmp_path, project, orchestrator, "--force-rewalk")
        assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-2000:])

        current = cr.current_revision()
        assert cr.read_resync_stamps(project) == {"kg": current, "codegraph": current}
        assert cr.read_resync_stamps(orchestrator) == {}, (
            "the orchestrator root's half must not be recorded by a walk of "
            "another project")
        # The update's own clear: the registry probe, through the doctor pass.
        assert doctor.reconcile_probe_cleared(project, log=lambda _l: None) == [CID]
        assert _cids(project) == []

    def test_a_plain_walk_leaves_the_entry(self, tmp_path):
        orchestrator = tmp_path / "orchestrator"
        orchestrator.mkdir()
        project = _project(tmp_path)
        _seed_entry(project)
        cr.record_resync_half(project, "kg")
        proc = _forced_walk(tmp_path, project, orchestrator)
        assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-2000:])
        assert cr.read_resync_stamps(project) == {"kg": cr.current_revision()}
        assert doctor.reconcile_probe_cleared(project, log=lambda _l: None) == []
        assert _cids(project) == [CID]

    def test_a_forced_walk_of_an_extra_folder_records_no_half(self, tmp_path):
        orchestrator = tmp_path / "orchestrator"
        orchestrator.mkdir()
        primary = _project(tmp_path, "primary")
        extra = _project(tmp_path, "extra")
        proc = _forced_walk(tmp_path, extra, orchestrator, "--force-rewalk",
                            "--as-extra-path", "--primary-root", str(primary))
        assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-2000:])
        for folder in (orchestrator, primary, extra):
            assert cr.read_resync_stamps(folder) == {}, folder.name


# ── 3. the heal for projects already stuck ──────────────────────────────────


class TestCompletedWalkEvidence:
    def test_a_completed_forced_walk_after_the_crossing_is_evidence(self, tmp_path):
        _crossed(tmp_path)
        _walked(tmp_path)
        assert cr.completed_walk_evidence(tmp_path) is True

    def test_an_accepted_failing_set_walk_is_evidence_too(self, tmp_path):
        _crossed(tmp_path)
        _walked(tmp_path, note="force-rewalk completed with an accepted failing set")
        assert cr.completed_walk_evidence(tmp_path) is True

    @pytest.mark.parametrize("note", ["version_at_or_past_bump", "no_graph", "", None])
    def test_a_stamp_set_without_a_walk_is_not(self, tmp_path, note):
        _crossed(tmp_path)
        _walked(tmp_path, note=note)
        assert cr.completed_walk_evidence(tmp_path) is False

    def test_an_older_generation_is_not(self, tmp_path):
        _crossed(tmp_path)
        _walked(tmp_path, generation="0.2.92")
        assert cr.completed_walk_evidence(tmp_path) is False

    def test_a_walk_that_finished_before_the_crossing_is_not(self, tmp_path):
        _crossed(tmp_path, at="2026-11-13T12:45:19Z")
        _walked(tmp_path, at="2026-11-13T12:45:18Z")
        assert cr.completed_walk_evidence(tmp_path) is False

    def test_a_sentinel_at_another_revision_is_not(self, tmp_path):
        _crossed(tmp_path, revision="v0.0.1-older")
        _walked(tmp_path)
        assert cr.completed_walk_evidence(tmp_path) is False

    def test_no_sentinel_or_no_walk_stamp_is_not(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        _walked(a)
        _crossed(b)
        assert cr.completed_walk_evidence(a) is False
        assert cr.completed_walk_evidence(b) is False

    def test_unparseable_times_are_not(self, tmp_path):
        _crossed(tmp_path, at="yesterday")
        _walked(tmp_path)
        assert cr.completed_walk_evidence(tmp_path) is False

    def test_a_pending_failing_set_is_not(self, tmp_path):
        """A held-back stamp keeps no ``force-rewalk completed`` note."""
        _crossed(tmp_path)
        _write_json(ceg.stamp_path(tmp_path), {
            "pending_failing_set": {"fingerprint": "x"},
            "written_at": "2026-11-13T12:58:40Z",
            "written_by": "vco_lib.codegraph_extractor_generation",
        })
        assert cr.completed_walk_evidence(tmp_path) is False


def _stuck(folder: Path) -> None:
    """A project in the field state: entry, KG half, walk done, no cg half."""
    _seed_entry(folder)
    _crossed(folder)
    cr.record_resync_half(folder, "kg")
    _walked(folder)


class TestTheProbeAcceptsTheEvidence:
    def test_a_stuck_project_is_provably_over(self, tmp_path):
        _stuck(tmp_path)
        assert cr.resync_still_owed(tmp_path) is False
        # read-only: the probe records nothing
        assert cr.read_resync_stamps(tmp_path) == {"kg": cr.current_revision()}

    def test_the_update_pass_clears_it(self, tmp_path):
        _stuck(tmp_path)
        assert doctor.reconcile_probe_cleared(tmp_path, log=lambda _l: None) == [CID]
        assert _cids(tmp_path) == []

    def test_walk_evidence_never_stands_in_for_the_kg_half(self, tmp_path):
        _seed_entry(tmp_path)
        _crossed(tmp_path)
        _walked(tmp_path)
        assert cr.resync_still_owed(tmp_path) is True

    def test_without_evidence_one_half_is_still_owed(self, tmp_path):
        _seed_entry(tmp_path)
        _crossed(tmp_path)
        cr.record_resync_half(tmp_path, "kg")
        _walked(tmp_path, note="version_at_or_past_bump")
        assert cr.resync_still_owed(tmp_path) is True
        assert doctor.reconcile_probe_cleared(tmp_path, log=lambda _l: None) == []
        assert _cids(tmp_path) == [CID]


class TestSessionStartHeal:
    @pytest.fixture(autouse=True)
    def _no_spawn(self, monkeypatch):
        self.spawned: list = []
        monkeypatch.setattr(deferral_retry, "spawn_detached",
                            lambda *a, **k: self.spawned.append(a))

    def test_a_stuck_project_clears_at_session_start_without_a_walk(self, tmp_path):
        _stuck(tmp_path)
        deferral_retry.session_start_owed_check(tmp_path)
        assert _cids(tmp_path) == []
        current = cr.current_revision()
        assert cr.read_resync_stamps(tmp_path) == {"kg": current, "codegraph": current}
        assert self.spawned == []

    def test_both_halves_recorded_clears_at_session_start(self, tmp_path):
        _seed_entry(tmp_path)
        cr.record_resync_half(tmp_path, "kg")
        cr.record_resync_half(tmp_path, "codegraph")
        assert cr.heal_stuck_resync(tmp_path) is True
        assert _cids(tmp_path) == []

    def test_no_evidence_leaves_entry_and_stamps_alone(self, tmp_path):
        _seed_entry(tmp_path)
        _crossed(tmp_path)
        cr.record_resync_half(tmp_path, "kg")
        _walked(tmp_path, at="2026-11-13T12:00:00Z")  # before the crossing
        before = cr.resync_state_path(tmp_path).read_bytes()
        deferral_retry.session_start_owed_check(tmp_path)
        assert _cids(tmp_path) == [CID]
        assert cr.resync_state_path(tmp_path).read_bytes() == before

    def test_the_kg_half_missing_keeps_the_entry_but_records_the_walk(self, tmp_path):
        _seed_entry(tmp_path)
        _crossed(tmp_path)
        _walked(tmp_path)
        assert cr.heal_stuck_resync(tmp_path) is False
        assert _cids(tmp_path) == [CID]
        assert cr.read_resync_stamps(tmp_path) == {"codegraph": cr.current_revision()}

    def test_a_project_without_the_entry_is_not_touched(self, tmp_path):
        _crossed(tmp_path)
        _walked(tmp_path)
        assert cr.heal_stuck_resync(tmp_path) is False
        assert not cr.resync_state_path(tmp_path).exists()

    def test_an_unreadable_stamp_file_is_left_alone(self, tmp_path):
        _stuck(tmp_path)
        cr.resync_state_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert cr.heal_stuck_resync(tmp_path) is False
        assert _cids(tmp_path) == [CID]
        assert cr.resync_state_path(tmp_path).read_text(encoding="utf-8") == "{not json"

    def test_the_clear_leaves_an_audit_row(self, tmp_path):
        _stuck(tmp_path)
        with mock.patch("vco_lib.deferral_probes.record_probe_resolution") as rec:
            assert cr.heal_stuck_resync(tmp_path) is True
        rec.assert_called_once_with(tmp_path, CID, "chunker_resync_still_owed")
