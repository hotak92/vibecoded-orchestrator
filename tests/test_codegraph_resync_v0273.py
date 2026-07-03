# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 R-track: resync trigger observability + owed-gating + verification.

R-5 (RT-2): detached children log to <vct_root_dir>/logs/resync-<proj>-<ts>.log
            instead of DEVNULL; the deferral resume command is shlex-quoted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import codegraph_resync as cr  # noqa: E402


def _stub_analyzer_tree(root: Path) -> None:
    scripts = root / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "analyze_code_graph.py").write_text("# stub\n")


class _FakeProc:
    pid = 4321


# ─────────────────────────── R-5: log file ───────────────────────────


def test_children_log_to_vct_root_logs_dir(monkeypatch, tmp_path):
    """stdout/stderr of BOTH detached children go to the per-spawn log file
    (not DEVNULL), created under <vct_root_dir>/logs/."""
    state_dir = tmp_path / "vct-state"
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    _stub_analyzer_tree(repo)

    spawned = []

    def _fake_popen(argv, **kwargs):
        spawned.append({"argv": argv, "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(cr.subprocess, "Popen", _fake_popen)
    result = cr.spawn_background_resync(
        repo, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    logs = list((state_dir / "logs").glob("resync-MyProj-*.log"))
    assert len(logs) == 1, "one per-spawn log file must exist"
    assert b"codegraph resync for MyProj" in logs[0].read_bytes()
    for rec in spawned:
        out = rec["kwargs"]["stdout"]
        assert out is not subprocess.DEVNULL, "children must not be DEVNULL'd"
        assert Path(out.name) == logs[0]
        assert rec["kwargs"]["stderr"] is out


def test_log_prep_failure_degrades_to_devnull(monkeypatch, tmp_path):
    """Log-path failure must never block the spawn — degrade to DEVNULL."""
    monkeypatch.setattr(cr, "_resync_log_path", lambda name: None)
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    _stub_analyzer_tree(repo)

    spawned = []

    def _fake_popen(argv, **kwargs):
        spawned.append(kwargs)
        return _FakeProc()

    monkeypatch.setattr(cr.subprocess, "Popen", _fake_popen)
    result = cr.spawn_background_resync(
        repo, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    assert all(k["stdout"] is subprocess.DEVNULL for k in spawned)


def test_log_filename_sanitizes_project_name(monkeypatch, tmp_path):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    p = cr._resync_log_path("weird name/with:chars")
    assert p is not None
    assert p.name.startswith("resync-weird_name_with_chars-")
    assert "/" not in p.name and ":" not in p.name


# ─────────────────── R-5 rider (A-1): quoted resume command ─────────────────


def test_resume_command_is_shlex_quoted(monkeypatch, tmp_path):
    """A repo path containing spaces must survive into the deferral command."""
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: False)
    repo = tmp_path / "my repo with spaces"
    repo.mkdir()
    _stub_analyzer_tree(repo)

    result = cr.spawn_background_resync(
        repo, "MyProj", python_exe="/usr/bin/python3", check_owed=False
    )
    assert result.status == "deferred"
    if result.deferral is not None:
        cmd = result.deferral.command_to_apply
        assert "'" in cmd or '"' in cmd, "spaced path must be quoted"
        # The quoted command round-trips through shlex to the same argv.
        import shlex

        parts = shlex.split(cmd)
        assert str(repo) in parts
        assert parts[-2:] == ["--project", "MyProj"]


# ─────────────────── R-6: gate the trigger on owed work ───────────────────


def test_embed_revision_parses_from_real_analyzer():
    """The regex parse of the shipped analyzer must agree with the constant
    the analyzer module actually defines (lock-step guard)."""
    import importlib.util
    import types as _t

    analyzer_path = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    parsed = cr._resolve_embed_revision(analyzer_path)

    spec = importlib.util.spec_from_file_location(
        "_r6_analyzer_probe", str(analyzer_path)
    )
    assert spec is not None and spec.loader is not None
    mod: _t.ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    assert parsed == mod.CODEGRAPH_EMBED_REVISION


def test_embed_revision_fallback_on_missing_or_garbage(tmp_path):
    assert cr._resolve_embed_revision(None) == cr._FALLBACK_EMBED_REVISION
    missing = tmp_path / "nope.py"
    assert cr._resolve_embed_revision(missing) == cr._FALLBACK_EMBED_REVISION
    garbage = tmp_path / "garbage.py"
    garbage.write_text("no anchor line here\n")
    assert cr._resolve_embed_revision(garbage) == cr._FALLBACK_EMBED_REVISION


class _AggColl:
    """Collection fake: filtered aggregate and/or embed_revision iterator."""

    def __init__(self, agg_count=None, agg_raises=False, rows=None,
                 iter_raises=False):
        import types as _t

        self.name = "X"
        self._rows = rows or []
        self._iter_raises = iter_raises
        if agg_raises:
            def _boom(**kw):
                raise RuntimeError("no null index")
            self.aggregate = _t.SimpleNamespace(over_all=_boom)
        else:
            self.aggregate = _t.SimpleNamespace(
                over_all=lambda **kw: _t.SimpleNamespace(total_count=agg_count)
            )

    def iterator(self, return_properties=None):
        import types as _t

        if self._iter_raises:
            raise RuntimeError("scan failed")
        for rev in self._rows:
            yield _t.SimpleNamespace(properties={"embed_revision": rev})


class _FakeClient:
    def __init__(self, colls):
        import types as _t

        self._colls = colls
        self.collections = _t.SimpleNamespace(
            exists=lambda name: name in colls,
            get=lambda name: colls[name],
        )
        self.closed = False

    def close(self):
        self.closed = True


def _client_for(prefix, module_coll, class_coll, func_coll):
    return _FakeClient({
        f"{prefix}_CodeModule": module_coll,
        f"{prefix}_CodeClass": class_coll,
        f"{prefix}_CodeFunction": func_coll,
    })


def test_count_stale_rows_aggregate_path(monkeypatch):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    client = _client_for(
        "Proj", _AggColl(agg_count=0), _AggColl(agg_count=2), _AggColl(agg_count=0)
    )
    counts = cr.count_stale_rows("Proj", current_revision=1, client=client)
    assert counts == {
        "Proj_CodeModule": 0, "Proj_CodeClass": 2, "Proj_CodeFunction": 0,
    }


def test_count_stale_rows_null_safe_scan_fallback(monkeypatch):
    """Aggregate unavailable (e.g. IsNull unindexed on an old collection) →
    the NULL-safe scan classifies NULL + mismatched revisions as stale."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    func = _AggColl(agg_raises=True, rows=[None, 0, 1, 1, "bad"])
    client = _client_for("Proj", _AggColl(agg_count=0), _AggColl(agg_count=0), func)
    counts = cr.count_stale_rows("Proj", current_revision=1, client=client)
    assert counts is not None
    assert counts["Proj_CodeFunction"] == 3  # NULL + 0 + unparseable


def test_count_stale_rows_absent_collection_counts_zero(monkeypatch):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    client = _FakeClient({})  # nothing exists
    counts = cr.count_stale_rows("Proj", current_revision=1, client=client)
    assert counts == {
        "Proj_CodeModule": 0, "Proj_CodeClass": 0, "Proj_CodeFunction": 0,
    }


def test_count_stale_rows_undeterminable_returns_none(monkeypatch):
    """Both tiers failing on one collection → the WHOLE probe is None
    (never a wrong zero)."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "Proj")
    broken = _AggColl(agg_raises=True, iter_raises=True)
    client = _client_for("Proj", _AggColl(agg_count=0), _AggColl(agg_count=0), broken)
    assert cr.count_stale_rows("Proj", current_revision=1, client=client) is None


def test_spawn_not_owed_when_probe_confirms_zero(monkeypatch, tmp_path):
    """POSITIVE zero from the probe → status not_owed, NOTHING spawned."""
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(
        cr, "count_stale_rows", lambda *a, **k: {"P_CodeFunction": 0}
    )
    _stub_analyzer_tree(tmp_path)

    def _no_spawn(*a, **k):
        raise AssertionError("nothing may spawn when no work is owed")

    monkeypatch.setattr(cr.subprocess, "Popen", _no_spawn)
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "not_owed"
    assert result.pid is None


def test_spawn_proceeds_when_probe_undeterminable(monkeypatch, tmp_path):
    """None from the probe (Weaviate down etc.) → proceed like pre-R-6
    (conservative: only a positive zero skips)."""
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    _stub_analyzer_tree(tmp_path)
    monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: _FakeProc())
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"


def test_spawn_proceeds_when_rows_owed(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(
        cr, "count_stale_rows", lambda *a, **k: {"P_CodeFunction": 7}
    )
    _stub_analyzer_tree(tmp_path)
    monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: _FakeProc())
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"


def test_install_shim_resolves_ledger_on_not_owed():
    """Source-level guard: install.py's shim handles not_owed by resolving
    the (deliberately foreign) resync ledger entry."""
    src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    assert 'result.status == "not_owed"' in src
    assert 'mark_resolved("codegraph_embed_resync_pending")' in src


# ─────────── R-7: driver runs analyzer, verifies, defers one-time ───────────


class _RunRecord:
    def __init__(self, returncode=0):
        self.calls = []
        self._rc = returncode

    def __call__(self, argv, **kwargs):
        import types as _t

        self.calls.append({"argv": argv, "kwargs": kwargs})
        return _t.SimpleNamespace(returncode=self._rc)


def _persisted_cids(folder: Path) -> set:
    from vco_lib.deferral_report import DeferralReport

    return {e.condition_id for e in DeferralReport.read(folder).entries}


def _seed_pending_deferral(folder: Path) -> None:
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    rep = DeferralReport.read(folder)
    rep.add_entry(DeferralEntry(
        condition_id="codegraph_embed_resync_pending",
        title="pending", detected="d", why_deferred="w",
        command_to_apply="cmd", severity="warning",
    ))
    rep.write(folder)


def test_driver_converged_resolves_persisted_deferral(monkeypatch, tmp_path):
    _seed_pending_deferral(tmp_path)
    rec = _RunRecord(returncode=0)
    monkeypatch.setattr(cr.subprocess, "run", rec)
    monkeypatch.setattr(
        cr, "count_stale_rows", lambda *a, **k: {"P_CodeFunction": 0}
    )
    rc = cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py"
    )
    assert rc == 0
    assert len(rec.calls) == 1
    assert "codegraph_embed_resync_pending" not in _persisted_cids(tmp_path), (
        "positive zero-stale must resolve the persisted ledger entry"
    )


def test_driver_unconverged_records_one_time_deferral(monkeypatch, tmp_path):
    rec = _RunRecord(returncode=0)
    monkeypatch.setattr(cr.subprocess, "run", rec)
    monkeypatch.setattr(
        cr, "count_stale_rows", lambda *a, **k: {"P_CodeFunction": 42}
    )
    rc = cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py",
        prune_stale=True,
    )
    assert rc == 0
    from vco_lib.deferral_report import DeferralReport

    rep = DeferralReport.read(tmp_path)
    assert rep.has_condition("codegraph_embed_resync_pending")
    entry = [e for e in rep.entries
             if e.condition_id == "codegraph_embed_resync_pending"][0]
    assert "42" in entry.detected
    # The resume command reruns the exact analyzer argv (incl. prune flag).
    assert "--prune-stale" in entry.command_to_apply
    assert "MyProj" in entry.command_to_apply
    # --prune-stale reached the analyzer argv too.
    assert "--prune-stale" in rec.calls[0]["argv"]


def test_driver_unverifiable_probe_still_surfaces(monkeypatch, tmp_path):
    """DESIGN-DECISION: unverifiable convergence must SURFACE (one-time
    deferral), never soft-fail into a void."""
    monkeypatch.setattr(cr.subprocess, "run", _RunRecord(returncode=0))
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py"
    )
    from vco_lib.deferral_report import DeferralReport

    rep = DeferralReport.read(tmp_path)
    assert rep.has_condition("codegraph_embed_resync_pending")
    entry = rep.entries[0]
    assert "could not be verified" in entry.detected


def test_driver_analyzer_crash_never_raises(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(cr.subprocess, "run", _boom)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    assert cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py"
    ) == 0


def test_spawn_launches_driver_not_bare_analyzer(monkeypatch, tmp_path):
    """The detached child is the --run-resync driver wrapping the analyzer."""
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_prune_stale_is_safe", lambda name: True)
    monkeypatch.setattr(cr, "_register_spawn_with_hub", lambda *a, **k: None)
    _stub_analyzer_tree(tmp_path)

    spawned = []
    monkeypatch.setattr(
        cr.subprocess, "Popen",
        lambda argv, **kw: (spawned.append(argv), _FakeProc())[1],
    )
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    driver = [a for a in spawned if "--run-resync" in a]
    assert len(driver) == 1
    argv = driver[0]
    assert "--repo-root" in argv and "--analyzer" in argv
    assert "--prune-stale" in argv, "safe project → prune forwarded"
    assert "--force-recreate" not in argv


def test_spawn_omits_prune_when_not_provably_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_prune_stale_is_safe", lambda name: False)
    monkeypatch.setattr(cr, "_register_spawn_with_hub", lambda *a, **k: None)
    _stub_analyzer_tree(tmp_path)
    spawned = []
    monkeypatch.setattr(
        cr.subprocess, "Popen",
        lambda argv, **kw: (spawned.append(argv), _FakeProc())[1],
    )
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    driver = [a for a in spawned if "--run-resync" in a][0]
    assert "--prune-stale" not in driver, (
        "prune must NOT run when extras cannot be ruled out"
    )


def test_spawn_registers_driver_pid_with_hub(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "code_embed_service_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: None)
    monkeypatch.setattr(cr, "_prune_stale_is_safe", lambda name: False)
    registered = []
    monkeypatch.setattr(
        cr, "_register_spawn_with_hub",
        lambda name, pid: registered.append((name, pid)),
    )
    _stub_analyzer_tree(tmp_path)
    monkeypatch.setattr(cr.subprocess, "Popen", lambda *a, **k: _FakeProc())
    result = cr.spawn_background_resync(
        tmp_path, "MyProj", python_exe="/usr/bin/python3"
    )
    assert result.status == "launched"
    assert registered == [("MyProj", 4321)]


def test_hub_registration_is_soft_when_hub_down(monkeypatch, tmp_path):
    """Closed port / missing token → no raise (observability, not a gate)."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "empty-state"))
    monkeypatch.setenv("VCT_HUB_PORT", "1")  # nothing listens on port 1
    monkeypatch.setenv("VCT_HUB_TOKEN", "t")
    cr._register_spawn_with_hub("MyProj", 123)  # must not raise


# ─────────────── R-7: prune-safety probe (destructive-action gate) ──────────


def _fake_db(monkeypatch, projects, extras_counts):
    """Wire a real in-memory sqlite DB through launcher_db_reader."""
    import sqlite3

    def _open():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE projects (id TEXT, name TEXT)")
        conn.execute(
            "CREATE TABLE project_codegraph_extra_paths "
            "(project_id TEXT, path TEXT)"
        )
        for pid, name in projects:
            conn.execute("INSERT INTO projects VALUES (?, ?)", (pid, name))
        for pid, n in extras_counts.items():
            for i in range(n):
                conn.execute(
                    "INSERT INTO project_codegraph_extra_paths VALUES (?, ?)",
                    (pid, f"/extra/{i}"),
                )
        return conn

    import vco_lib.launcher_db_reader as ldr

    monkeypatch.setattr(ldr, "_open_db_readonly", _open)


def test_prune_safe_when_zero_extras(monkeypatch):
    _fake_db(monkeypatch, [("p1", "MyProj")], {})
    assert cr._prune_stale_is_safe("MyProj") is True


def test_prune_unsafe_when_extras_exist(monkeypatch):
    """LEAVE-ALONE branch of the destructive gate: extras rows would be
    deleted by a prune the walk can't cover → never prune."""
    _fake_db(monkeypatch, [("p1", "MyProj")], {"p1": 2})
    assert cr._prune_stale_is_safe("MyProj") is False


def test_prune_unsafe_when_project_unknown_or_ambiguous(monkeypatch):
    _fake_db(monkeypatch, [], {})
    assert cr._prune_stale_is_safe("MyProj") is False
    _fake_db(
        monkeypatch,
        [("p1", "My Proj"), ("p2", "MyProj")],  # both sanitize to MyProj
        {},
    )
    assert cr._prune_stale_is_safe("MyProj") is False


def test_prune_unsafe_when_db_unavailable(monkeypatch):
    import vco_lib.launcher_db_reader as ldr

    monkeypatch.setattr(ldr, "_open_db_readonly", lambda: None)
    assert cr._prune_stale_is_safe("MyProj") is False
