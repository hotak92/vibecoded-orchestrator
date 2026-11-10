# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — the update defects a Windows user hit on v0.2.105.

Each test is a RED PROOF: it fails on the v0.2.105 source and passes after
the fix in this cycle. The tests are grouped by the field-report item.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import console_text  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────


def _load_script(rel: str, tag: str):
    """Import a shipped script by path (its own bootstrap runs, no network)."""
    path = REPO_ROOT / rel
    name = f"_v02106_{tag}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # pragma: no cover — partial install
        pytest.skip(f"{path.name} has runtime deps not installed ({exc})")
    return mod


class _Cp1252Stream:
    """A Windows-like stream: refuses every character cp1252 cannot encode."""

    def __init__(self) -> None:
        self.data = ""

    def write(self, text: str) -> int:
        text.encode("cp1252")  # UnicodeEncodeError on an arrow / em dash
        self.data += text
        return len(text)

    def flush(self) -> None:
        return None


# ── item 1: hand-to-vco console output (cp1252) ─────────────────────────────


def test_console_print_transliterates_instead_of_raising():
    stream = _Cp1252Stream()
    assert console_text.console_print("stop → remove", file=stream) is True
    assert "stop -> remove" in stream.data


def test_console_print_swallows_an_io_failure():
    class _Broken:
        def write(self, _text: str) -> int:
            raise OSError("pipe closed")

    assert console_text.console_print("anything", file=_Broken()) is False


def test_adoption_default_log_survives_the_cp1252_arrow():
    """The reported shape: the "stop → remove" arrow aborted the hand-over."""
    from vco_lib import console_text, service_adoption as sa

    assert sa._default_log() is console_text.console_print
    stream = _Cp1252Stream()
    with mock.patch("sys.stdout", stream):
        sa._default_log()("  [adopt:weaviate] stop → remove container → up")
    assert "stop -> remove" in stream.data


# ── item 2: PowerShell probe classification ─────────────────────────────────


def _completed(major: int, minor: int):
    return subprocess.CompletedProcess([], 0, f"{major}\n{minor}\n", "")


def test_powershell_missing_is_missing():
    from vco_lib import windows_powershell as wp

    answer = wp.probe(which=lambda _n: None)
    assert answer.state == wp.STATE_MISSING
    assert answer.version is None


def test_powershell_timeout_is_retried_then_reported_as_no_answer():
    """An 8 s timeout must NOT be reported as "not installed" (field report)."""
    from vco_lib import windows_powershell as wp

    calls = []

    def _run(argv, **kw):
        calls.append(kw["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return _completed(5, 1)

    answer = wp.probe(which=lambda _n: "/usr/bin/powershell", run=_run)
    assert answer.state == wp.STATE_OK
    assert answer.version == (5, 1)
    assert calls == [wp.DEFAULT_TIMEOUT_S, wp.DEFAULT_RETRY_TIMEOUT_S]


def test_powershell_answering_twice_never_is_no_answer_not_missing():
    from vco_lib import windows_powershell as wp

    def _run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    answer = wp.probe(which=lambda n: f"/usr/bin/{n}", run=_run)
    assert answer.state == wp.STATE_NO_ANSWER
    assert "did not answer" in answer.detail


def test_powershell_too_old_names_the_version():
    from vco_lib import windows_powershell as wp

    answer = wp.probe(which=lambda _n: "/usr/bin/powershell",
                      run=lambda *_a, **_k: _completed(4, 0))
    assert answer.state == wp.STATE_TOO_OLD
    assert answer.version_text() == "4.0"


# ── item 3a: sync and doctor agree on the project's OWN collection ──────────


def test_hub_down_primary_comes_from_the_launcher_binding(tmp_path, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    from tests.common.launcher_db_fixture import add_project, make_launcher_db

    db = make_launcher_db(tmp_path / "db")
    add_project(
        db, project_id="pid-106", name="Proj 106", folder_path=str(folder),
        host="base", kg_primary="Proj106_KnowledgeGraph",
        kg_shared="Machine_Shared_KG",
    )
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    # A stale/foreign env value is exactly what made the sync read the shared
    # class while the doctor's binding-based census reported the notes
    # disk-only.
    monkeypatch.setenv("KG_COLLECTION", "Machine_Shared_KG")
    monkeypatch.setenv("KG_BASE_DIR", str(folder))
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)

    mod = _load_script("templates/scripts/sync_knowledge_graph.py", "sync")
    with mock.patch("vco_lib.project_config.resolve",
                    side_effect=RuntimeError("hub down")):
        kg, _dev, shared, resolved = mod._resolve_collections()
    assert kg == "Proj106_KnowledgeGraph", (
        "the hub-down tier must use the launcher binding — the same rule the "
        "doctor's drift census uses")
    assert shared == "Machine_Shared_KG"
    assert resolved is True


def test_hub_down_unregistered_folder_never_takes_the_roots_primary(
    tmp_path, monkeypatch,
):
    """Review C-H1: the orchestrator-root fallback is for the SHARED role only.

    An unregistered folder (CLI-added, mid-move) on a hub-down run must keep
    its env ``KG_COLLECTION`` as primary — the root's own private class is
    never a foreign project's sync target — while the machine-wide shared
    class still comes from the root row.
    """
    folder = tmp_path / "unregistered"
    folder.mkdir()
    monkeypatch.setenv("KG_COLLECTION", "Foo_KnowledgeGraph")
    monkeypatch.setenv("KG_BASE_DIR", str(folder))
    monkeypatch.delenv("SHARED_KG_COLLECTION", raising=False)
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)

    mod = _load_script("templates/scripts/sync_knowledge_graph.py", "sync_unreg")
    with mock.patch("vco_lib.project_config.resolve",
                    side_effect=RuntimeError("hub down")), \
         mock.patch("vco_lib.module_gated_delivery.resolve_project_id_for_folder",
                    return_value=None), \
         mock.patch("vco_lib.launcher_db_reader.get_orchestrator_root_bindings",
                    return_value=("Root_KnowledgeGraph", "Machine_Shared_KG")):
        assert mod._kg_binding_from_launcher_db(folder, "primary") == ""
        assert mod._kg_binding_from_launcher_db(folder, "shared") == (
            "Machine_Shared_KG")
        kg, _dev, shared, resolved = mod._resolve_collections()
    assert kg == "Foo_KnowledgeGraph", (
        "an unregistered folder must never sync into the root's primary class")
    assert shared == "Machine_Shared_KG"
    assert resolved is True


# ── item 3b: the summary refresh forwards progress ──────────────────────────


class _FakeChild:
    """A generator child that prints one progress line, then exits 0."""

    def __init__(self, *args, **kwargs) -> None:
        self.stdout = iter(["[1/1] OK    knowledge/a.md  (0.1s)\n"])
        self.stderr = iter([])
        self.returncode = 0
        self._done = False

    def poll(self):
        return 0 if self._done else None

    def wait(self, timeout=None):
        self._done = True
        return 0

    def communicate(self, timeout=None):
        self._done = True
        return ("[1/1] OK    knowledge/a.md  (0.1s)\n", "")


def test_summary_refresh_forwards_the_childs_progress(tmp_path, monkeypatch, capsys):
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "a.md").write_text("---\ntitle: a\n---\n", encoding="utf-8")
    root = tmp_path / "root"
    (root / "templates" / "scripts").mkdir(parents=True)
    (root / "templates" / "scripts" / "generate-kg-summary.py").write_text(
        "", encoding="utf-8")

    mod = _load_script("templates/scripts/sync_knowledge_graph.py", "sync2")
    monkeypatch.setattr(mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(mod, "KNOWLEDGE_ROOT", knowledge)
    monkeypatch.setattr(mod, "_emit_sync_event", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_emit_node_formats_no_backend_deferral", lambda *a: None)
    monkeypatch.setattr(mod, "_emit_node_formats_deferral", lambda *a, **k: None)
    monkeypatch.setattr("vco_lib.python_exe.resolve_install_root", lambda: root)
    monkeypatch.setattr(subprocess, "Popen", _FakeChild)

    mod._regen_node_formats_after_full_sync()
    out = capsys.readouterr().out
    assert "[1/1] OK" in out, (
        "the generator's per-node progress must reach the user; a 28-minute "
        "silent wait was the field report")


# ── item 4a: an EMPTY named-vector slot is not a populated one ──────────────


def test_duplicate_scan_treats_an_empty_slot_vector_as_missing():
    mod = _load_script("templates/scripts/detect_duplicates.py", "dups")

    class _Obj:
        vector = {"arctic2_embed": [], "qwen3_embed": [0.1, 0.2]}

    class _Collection:
        class query:
            @staticmethod
            def fetch_object_by_id(_uid, include_vector=False):
                return _Obj()

    det = mod.DuplicateDetector.__new__(mod.DuplicateDetector)
    det.collection = _Collection()
    slots = det._object_vector_slots("u-1")
    assert slots == ("qwen3_embed",), (
        "a slot KEY with an empty vector is not a populated slot; counting it "
        "made one object abort the whole scan")


# ── item 4b: the supported backfill refuses another model's slot ────────────


def test_backfill_refuses_a_slot_that_is_not_the_models(monkeypatch, capsys):
    from vco_lib import embedding_enrichment as ee

    class _Svc:
        text_vector_slot = "qwen3_embed"
        text_model_id = "qwen3-embedding:0.6b"

        def close(self):
            pass

    monkeypatch.setattr(
        ee.EmbeddingService, "for_project",
        classmethod(lambda cls, **kw: _Svc()),
    )
    rc = ee.main(["backfill", "--collection", "Proj_KnowledgeGraph",
                  "--slot", "arctic2_embed"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert rc == 1
    assert payload["error"] == "SlotNotInSchemaError"
    assert "not the slot of this project's selected text model" in payload["message"]


def test_backfill_reuses_enrich_with_the_models_own_slot(monkeypatch, capsys):
    from vco_lib import embedding_enrichment as ee

    class _Svc:
        text_vector_slot = "arctic2_embed"
        text_model_id = "snowflake-arctic-embed2:latest"

        def close(self):
            pass

    seen = {}

    def _enrich(**kw):
        seen.update(kw)
        return ee.EnrichmentReport(
            collection=kw["collection_name"], new_slot=kw["new_slot"], total=60,
            enriched=60, skipped=0, failed=0, failures=[],
        )

    monkeypatch.setattr(
        ee.EmbeddingService, "for_project",
        classmethod(lambda cls, **kw: _Svc()),
    )
    monkeypatch.setattr(ee, "enrich_collection_vectors", _enrich)
    rc = ee.main(["backfill", "--collection", "Proj_KnowledgeGraph"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert rc == 0
    assert seen["new_slot"] == "arctic2_embed"
    assert payload["slot"] == "arctic2_embed"


# ── item 5: the catch-up runner always logs its outcome ─────────────────────


def test_catchup_runner_logs_the_outcome_even_when_nothing_else_is_written(
        tmp_path, monkeypatch):
    from vco_lib import codegraph_head_catchup as chc

    monkeypatch.setattr(chc, "catchup",
                        lambda *a, **kw: "analyzer_failed")
    rc = chc.main(["--project-root", str(tmp_path), "--analyzer", "/x/a.py"])
    assert rc == 0
    log = tmp_path / ".claude" / "logs" / chc.LOG_NAME
    assert log.is_file(), "a failed run must leave a log, never 0 bytes"
    text = log.read_text(encoding="utf-8")
    assert "run start:" in text
    assert "outcome=analyzer_failed" in text


def test_a_raising_catchup_is_logged_with_its_reason(tmp_path, monkeypatch):
    from vco_lib import codegraph_head_catchup as chc

    def _boom(*a, **kw):
        raise RuntimeError("hub exploded")

    monkeypatch.setattr(chc, "catchup", _boom)
    assert chc.main(["--project-root", str(tmp_path), "--analyzer", "/x/a.py"]) == 0
    text = (tmp_path / ".claude" / "logs" / chc.LOG_NAME).read_text(encoding="utf-8")
    assert "hub exploded" in text


# ── item 6: launcher_binary_fresh is not "problem" while it runs ────────────


def _freshness_probe(monkeypatch, running: bool):
    from vco_lib import deferral_probes as dp

    monkeypatch.setattr(dp, "_dist_dirty", lambda *a, **k: False)
    monkeypatch.setattr(dp, "_on_disk_launcher_version", lambda *a, **k: "1.2.3")
    monkeypatch.setattr(dp, "version_reached", lambda *a, **k: True)
    monkeypatch.setattr(dp, "_launcher_process_running", lambda _n: running)
    return dp.ProbeContext(
        folder=Path("/tmp/proj"), entry=None,
        extras={"dist_rel_dir": "launcher/dist/linux", "launcher_binary_name": "vco",
                "source_version": "1.2.3"},
    )


def test_a_running_launcher_is_not_stale(monkeypatch):
    from vco_lib import deferral_probes as dp

    ctx = _freshness_probe(monkeypatch, running=True)
    assert dp.launcher_binary_freshness(ctx) == dp.LAUNCHER_RUNNING_UNVERIFIED
    # The deferral CLEAR probe stays conservative (the entry is not retired).
    assert dp.launcher_binary_stale_still_applies(ctx) is True


def test_doctor_reports_a_running_launcher_as_unknown_not_problem(monkeypatch):
    from vco_lib import doctor, deferral_probes as dp

    ctx = _freshness_probe(monkeypatch, running=True)
    monkeypatch.setattr(dp, "launcher_binary_freshness",
                        lambda _ctx: dp.LAUNCHER_RUNNING_UNVERIFIED)
    findings = doctor.probe_launcher_binary_fresh(
        Path("/tmp/proj"), doctor.DoctorResolvers(),
        {"launcher_probe_extras": ctx.extras})
    assert [f.status for f in findings] == [doctor.STATUS_UNKNOWN]


def test_a_slow_probe_is_reported_not_checked_not_as_a_failure(monkeypatch):
    from vco_lib import doctor

    def _slow(_folder, _res, _ctx):
        time.sleep(3)
        return [doctor.Finding(probe="slow", status=doctor.STATUS_PROBLEM,
                               summary="should not be reached in time")]

    def _fast(_folder, _res, _ctx):
        return [doctor.Finding(probe="fast", status=doctor.STATUS_OK, summary="ok")]

    monkeypatch.setenv("VCT_DOCTOR_CHECK_TIMEOUT_S", "1")
    monkeypatch.setattr(doctor, "PROBES", {"slow": (_slow, (doctor.SCOPE_FULL,)),
                                           "fast": (_fast, (doctor.SCOPE_FULL,))})
    started = time.monotonic()
    report = doctor.run_doctor(Path("/tmp/proj"))
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, "the per-check bound must actually bound the pass"
    slow = [f for f in report.findings if f.probe == "slow"]
    assert len(slow) == 1 and slow[0].status == doctor.STATUS_UNKNOWN
    assert "not checked" in slow[0].summary
    assert any(f.probe == "fast" for f in report.findings)


def test_an_abandoned_probe_writes_nothing_afterwards(tmp_path, monkeypatch):
    """Review C-M3: a probe past its bound is CANCELLED — when its thread
    finally reaches the ledger writer, nothing is written."""
    import threading

    from vco_lib import deferral_emit, doctor
    from vco_lib.deferral_report import DeferralEntry

    release = threading.Event()

    def _late_writer(folder, _res, _ctx):
        release.wait(30)  # still running when the doctor gives up
        with deferral_emit.locked_report(folder) as report:
            report.add_entry(DeferralEntry(
                condition_id="late_probe_write", title="late",
                detected="written after the bound", why_deferred="x",
                command_to_apply="true", severity="info", kg_node_refs=[]))
        return []

    project = tmp_path / "proj"
    (project / ".claude" / "context").mkdir(parents=True)
    monkeypatch.setenv("VCT_DOCTOR_CHECK_TIMEOUT_S", "1")
    monkeypatch.setattr(doctor, "PROBES", {"late": (_late_writer, (doctor.SCOPE_FULL,))})
    report = doctor.run_doctor(project)
    [late] = report.findings
    assert late.status == doctor.STATUS_UNKNOWN and "writes nothing" in late.summary
    release.set()
    for t in threading.enumerate():
        if t.name == "doctor-probe-late":
            t.join(10)
    assert not (project / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()
    # The same writer outside a probe writes as before.
    _late_writer(project, None, None)
    assert (project / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()


def test_a_census_past_its_deadline_reports_the_rest_and_writes_nothing(
        tmp_path, monkeypatch):
    """Review C-M3: the census stops starting projects at its deadline; the
    rest are ``unknown/not_checked_time_bound`` and an incomplete census
    writes neither its state nor the ledger."""
    from vco_lib import bundle_staleness as bs
    from vco_lib import launcher_db_reader

    class _Ref:
        def __init__(self, i):
            self.id, self.name, self.folder = f"p{i}", f"P{i}", str(tmp_path / f"p{i}")

    monkeypatch.setattr(launcher_db_reader, "list_registered_projects",
                        lambda: [_Ref(1), _Ref(2)])
    written = []
    monkeypatch.setattr(bs, "_write_census_state", lambda *a, **k: written.append("state"))
    monkeypatch.setattr(bs, "refresh_root_ledger", lambda *a, **k: written.append("ledger"))
    payload = bs.run_census(tmp_path, deadline=time.monotonic() - 1)
    assert payload["not_checked"] == 2
    assert {r["reason"] for r in payload["projects"]} == {bs.REASON_NOT_CHECKED}
    assert written == []
    payload = bs.run_census(tmp_path)  # no deadline: complete, writes as before
    assert payload["not_checked"] == 0 and written == ["state", "ledger"]


def test_a_partial_census_is_never_an_ok_that_clears_the_entry(monkeypatch, tmp_path):
    from vco_lib import bundle_staleness as bs
    from vco_lib import doctor, paths

    monkeypatch.setattr(paths, "looks_like_orchestrator_root", lambda _p: True)
    monkeypatch.setattr(bs, "run_census", lambda *a, **k: {
        "registry": "launcher.db", "running": {"version": "x"}, "remedy": {},
        "summary": {"stale": 0, "unknown": 1, "current": 1}, "not_checked": 1,
        "projects": [{"name": "A", "verdict": "current"},
                     {"name": "B", "verdict": "unknown",
                      "reason": bs.REASON_NOT_CHECKED}]})
    [f] = doctor.probe_bundle_staleness(tmp_path, doctor.DoctorResolvers(), {})
    assert f.status == doctor.STATUS_UNKNOWN
    assert f.condition_id in (None, "")
    assert "1 not checked" in f.summary


# ── item 7: metrics migration compares content, not bytes ───────────────────


def test_a_concurrent_append_is_not_reported_as_a_failed_copy(tmp_path, monkeypatch):
    """The reported 14/66-byte warnings: every line matched, the byte count
    did not, because a hook appended a row between our write and our
    measure. Content equality is the verdict; byte arithmetic is a note."""
    from vco_lib import metrics_migration as mm

    src = tmp_path / "a.jsonl"
    dst = tmp_path / "b.jsonl"
    src.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    dst.write_bytes(b"")

    real_append = mm._append_lines

    def _append_then_concurrent(path, lines):
        written = real_append(path, lines)
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"concurrent": 1}\n')  # another writer's row
        return written

    monkeypatch.setattr(mm, "_append_lines", _append_then_concurrent)
    rec = mm._copy_one(src, dst, None, dry_run=False)
    assert rec.verified is True and not rec.error, (
        "the same rows are present; a concurrent append is not a lost row")


def test_a_real_missing_row_still_fails_the_verification(tmp_path, monkeypatch):
    from vco_lib import metrics_migration as mm

    src = tmp_path / "a.jsonl"
    dst = tmp_path / "b.jsonl"
    src.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    dst.write_bytes(b"")

    def _append_nothing(path, lines):
        return 0

    monkeypatch.setattr(mm, "_append_lines", _append_nothing)
    rec = mm._copy_one(src, dst, None, dry_run=False)
    assert rec.verified is False
    assert "source row(s) missing" in rec.error


# ── item 8: py-compile-check covers Edit as well as Write ───────────────────


def test_py_compile_check_is_registered_on_edit_and_write():
    from vco_lib import edit_post_check as epc

    for tmpl in ("linux", "windows"):
        data = json.loads(
            (REPO_ROOT / "templates" / f"settings.json.{tmpl}.template")
            .read_text(encoding="utf-8"))
        matchers = [
            group.get("matcher")
            for group in data["hooks"]["PostToolUse"]
            for hook in group["hooks"]
            if "py-compile-check" in hook.get("command", "")
        ]
        assert matchers == ["Edit|Write"], (tmpl, matchers)
    assert epc.covering_checker("Edit", "/p/m.py", "/p") == "py_compile"


def _py_compile_groups(doc: dict) -> list:
    return [g for g in doc["hooks"]["PostToolUse"]
            if any("py-compile-check" in h.get("command", "") for h in g["hooks"])]


def _v02105_settings(tmpl: str) -> dict:
    """The current template with the py-compile-check group as v0.2.105
    shipped it (matcher ``Write``; command and keys unchanged) — what an
    installed project carries into this update."""
    doc = json.loads((REPO_ROOT / "templates" / f"settings.json.{tmpl}.template")
                     .read_text(encoding="utf-8"))
    (group,) = _py_compile_groups(doc)
    group["matcher"] = "Write"
    return doc


@pytest.mark.parametrize("tmpl", ["linux", "windows"])
def test_an_updated_project_moves_py_compile_check_onto_edit(tmpl):
    """Review C-M1, real order: v0.2.105 settings → bundle-update merge →
    exactly one ``Edit|Write`` group, no leftover ``Write`` group; a fixed
    point; and a user's own ``Write`` group is left exactly as it is."""
    import copy

    from vco_lib import settings_merge

    template = json.loads(
        (REPO_ROOT / "templates" / f"settings.json.{tmpl}.template")
        .read_text(encoding="utf-8"))
    installed = _v02105_settings(tmpl)
    own = {"matcher": "Write",
           "hooks": [{"type": "command", "command": "bash ~/my-write-hook.sh"}]}
    installed["hooks"]["PostToolUse"].append(copy.deepcopy(own))

    merged = settings_merge.smart_merge_settings(installed, template)
    groups = _py_compile_groups(merged)
    assert [g["matcher"] for g in groups] == ["Edit|Write"], groups
    assert groups == _py_compile_groups(template)
    assert own in merged["hooks"]["PostToolUse"], "the user's own group moved"
    again = settings_merge.smart_merge_settings(copy.deepcopy(merged), template)
    assert again == merged


def test_a_user_group_mixing_py_compile_check_keeps_its_matcher():
    """A ``Write`` group that also carries the user's own hook is theirs."""
    import copy

    from vco_lib import settings_merge

    template = json.loads(
        (REPO_ROOT / "templates" / "settings.json.linux.template")
        .read_text(encoding="utf-8"))
    installed = _v02105_settings("linux")
    (group,) = _py_compile_groups(installed)
    group["hooks"].append({"type": "command", "command": "bash ~/mine.sh"})
    merged = settings_merge.smart_merge_settings(installed, template)
    mixed = [g for g in merged["hooks"]["PostToolUse"]
             if any(h.get("command") == "bash ~/mine.sh" for h in g["hooks"])]
    assert len(mixed) == 1 and mixed[0]["matcher"] == "Write"
    assert settings_merge.smart_merge_settings(copy.deepcopy(merged), template) == merged


def test_both_hook_siblings_filter_by_extension():
    sh = (REPO_ROOT / "templates" / "hooks" / "py-compile-check.sh").read_text(
        encoding="utf-8")
    ps1 = (REPO_ROOT / "templates" / "hooks" / "py-compile-check.ps1").read_text(
        encoding="utf-8")
    assert 'fp.endswith(".py")' in sh
    assert "EndsWith('.py')" in ps1