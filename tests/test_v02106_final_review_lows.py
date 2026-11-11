# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 FINAL review (Fable) — the LOW findings, one pin each.

* L1 — Remove from VCO takes out the copies the provider switch parked
  (they carry no ``files`` hash; their proof is the shipped template);
* L2 — a bundle census the doctor's bound cancels hands back the stale
  projects it found and names the unbounded census;
* L3 — a dismissal's manifest write runs under the bundle-manifest lock;
* L4 — the Weaviate case-history ledger's read-modify-writes are locked;
* L8 — the engine's wait for the manifest lock is visible and bounded;
* L9 — the edit poller does not wait for a checker the settings never run;
* L10 — the restore gate's key answer reaches the delivery selection; an
  unreadable registry names the drop command that still works; a skipped
  ledger write resolves nothing.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from vco_lib.atomic import LockTimeout, exclusive_file_lock
from vco_lib.hashing import sha256_bytes

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── L1 ────────────────────────────────────────────────────────────────────


def _parked_project(tmp_path: Path, parked_bytes: bytes) -> Path:
    folder = tmp_path / "proj"
    dis = folder / ".claude" / "agents.disabled"
    dis.mkdir(parents=True)
    (dis / "kimi-reviewer.md").write_bytes(parked_bytes)
    manifest = {"schema_version": 2, "files": {}, "preserved_files": {}, "packs": {},
                "provider_parked_agents": {".claude/agents/kimi-reviewer.md": "kimi"}}
    (folder / ".claude" / ".vco-manifest.json").write_text(json.dumps(manifest))
    return folder


def test_remove_takes_out_a_provider_parked_copy_of_the_shipped_bytes(tmp_path):
    from vco_lib import unregister_files

    shipped = (REPO_ROOT / "templates" / "agents" / "module-gateway"
               / "kimi-reviewer.md").read_bytes()
    folder = _parked_project(tmp_path, shipped)
    out = unregister_files.purge_project_files(folder, orchestrator_root=REPO_ROOT)
    assert not (folder / ".claude/agents.disabled/kimi-reviewer.md").exists()
    assert ".claude/agents.disabled/kimi-reviewer.md" in out["removed"]
    assert out["errors"] == []


def test_remove_keeps_and_names_a_parked_copy_it_cannot_prove(tmp_path):
    from vco_lib import unregister_files

    folder = _parked_project(tmp_path, b"edited while parked\n")
    out = unregister_files.purge_project_files(folder, orchestrator_root=REPO_ROOT)
    assert (folder / ".claude/agents.disabled/kimi-reviewer.md").exists()
    assert ".claude/agents.disabled/kimi-reviewer.md" in out["kept_modified"]


# ── L2 ────────────────────────────────────────────────────────────────────


def test_a_cancelled_census_hands_back_what_it_found(tmp_path, monkeypatch):
    from vco_lib import bundle_staleness, doctor, probe_bound

    stale_row = {"verdict": "stale", "name": "Alpha", "folder": "/w/alpha",
                 "changed_files": ["a", "b"]}
    gate = threading.Event()

    def slow_census(root, **kw):
        kw["on_row"](stale_row)          # project 1 classified
        gate.wait(5)                      # project 2 outlasts the bound
        return {"summary": {}, "projects": [stale_row]}

    monkeypatch.setattr(bundle_staleness, "run_census", slow_census)
    monkeypatch.setattr("vco_lib.paths.looks_like_orchestrator_root", lambda _r: True)
    try:
        findings = doctor._run_probe_bounded(
            "bundle_staleness", doctor.probe_bundle_staleness, tmp_path,
            doctor.DoctorResolvers(), {}, 0.5)
    finally:
        gate.set()
    head, *rest = findings
    assert head.status == doctor.STATUS_UNKNOWN and "not checked" in head.summary
    assert "python -m vco_lib.bundle_staleness" in head.summary
    assert "1 partial result" in head.summary
    assert [f.summary for f in rest] == ["Alpha: 2 bundle file(s) differ from what an update would ship"]
    assert not any(f.condition_id for f in rest), "a partial census clears nothing"
    assert probe_bound.deadline() is None  # the worker's state never leaks


# ── L3 ────────────────────────────────────────────────────────────────────


def test_a_dismissal_writes_the_manifest_under_the_bundle_lock(tmp_path, monkeypatch):
    from vco_lib import deferral_dismissal, project_init
    from vco_lib.bundle_manifest_lock import LOCK_REL

    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    held: list[bool] = []
    real = project_init._write_manifest_atomic

    def probe(target, manifest):
        try:
            with exclusive_file_lock(target / LOCK_REL, timeout_s=0.05):
                held.append(False)
        except LockTimeout:
            held.append(True)
        real(target, manifest)

    monkeypatch.setattr(project_init, "_write_manifest_atomic", probe)
    assert deferral_dismissal.record_dismissal(folder, "some_condition", {})
    assert held == [True]


# ── L4 ────────────────────────────────────────────────────────────────────


def test_every_case_history_write_holds_the_ledger_lock(tmp_path, monkeypatch):
    from vco_lib import weaviate_case_history as wch
    from vco_lib import weaviate_raft_guard as rg

    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(rg, "read_raft_stats", lambda *_a, **_k: rg.RaftStats(9, 0, 9))
    held: list[bool] = []
    real = wch._save

    def probe(data):
        lock = wch.ledger_path().with_name(wch.ledger_path().name + ".lock")
        try:
            with exclusive_file_lock(lock, timeout_s=0.05):
                held.append(False)
        except LockTimeout:
            held.append(True)
        real(data)

    monkeypatch.setattr(wch, "_save", probe)
    url = "http://127.0.0.1:18080"
    wch.record_deleted("A_Kg", url)
    wch.delete_index_bounds(url, ["B_Kg"], 12)
    wch._forget(url, ["A_Kg"])
    assert held == [True, True, True]


# ── L8 ────────────────────────────────────────────────────────────────────


def test_the_engine_says_it_waits_and_gives_up_with_a_clear_error(tmp_path, monkeypatch, capsys):
    from vco_lib import bundle_manifest_lock as bml

    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    monkeypatch.setattr(bml, "ENGINE_NOTICE_FIRST_S", 0.05)
    monkeypatch.setattr(bml, "ENGINE_NOTICE_EVERY_S", 0.05)
    monkeypatch.setattr(bml, "ENGINE_WAIT_S", 0.2)
    ran: list[int] = []

    @bml.serialized_bundle_run
    def engine(target, **_kw):
        ran.append(1)

    with bml.bundle_manifest_lock(folder):
        with pytest.raises(bml.BundleRunBusy) as exc:
            engine(folder)
    assert ran == []
    assert "bundle-manifest.lock" in str(exc.value) and "changed nothing" in str(exc.value)
    assert "waiting for another bundle run" in capsys.readouterr().err
    engine(folder)  # free again: runs at once
    assert ran == [1]


def test_a_lock_timeout_inside_the_run_is_not_read_as_a_busy_lock(tmp_path):
    from vco_lib import bundle_manifest_lock as bml

    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)

    @bml.serialized_bundle_run
    def engine(target, **_kw):
        raise LockTimeout("the body's own lock")

    with pytest.raises(LockTimeout) as exc:
        engine(folder)
    assert not isinstance(exc.value, bml.BundleRunBusy)


# ── L9 ────────────────────────────────────────────────────────────────────


def _settings(folder: Path, groups: list) -> None:
    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    (folder / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": groups}}), encoding="utf-8")


_PCC = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/py-compile-check.sh"'


@pytest.mark.parametrize("groups, expect", [
    # a v0.2.105 Write group that also carries the user's own hook
    ([{"matcher": "Write", "hooks": [{"type": "command", "command": _PCC},
                                     {"type": "command", "command": "my-linter"}]}], "inline"),
    # disabled in the launcher: no registration at all
    ([{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "my-linter"}]}], "inline"),
    # the shipped shape: the poller owns the row
    ([{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": _PCC}]}], "detached"),
])
def test_an_edit_is_not_polled_for_a_checker_the_settings_never_run(tmp_path, groups, expect):
    from vco_lib import edit_post_check as epc

    folder = tmp_path / "proj"
    _settings(folder, groups)
    rows: list[dict] = []
    outcome = epc.record_edit_outcome(
        project_root=str(folder), session_id="s", agent_id="", tool_name="Edit",
        file_path=str(folder / "m.py"), diff_size=3, file_existed_before=True,
        ts_ms=int(time.time() * 1000), emit=lambda **kw: rows.append(kw) or True,
        spawn=lambda *_a: None)
    assert outcome == expect
    if expect == "inline":
        assert rows[0]["payload"]["post_check"] == epc.no_checker_post_check()


def test_unreadable_settings_keep_the_table_answer(tmp_path):
    from vco_lib import edit_post_check as epc

    assert epc.registered_for("py_compile", "Edit", str(tmp_path)) is None
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    assert epc.registered_for("py_compile", "Edit", str(tmp_path)) is None


# ── L3 ────────────────────────────────────────────────────────────────────


def test_a_commented_settings_file_is_read_not_skipped(tmp_path):
    """Review L3: settings files are JSONC. With a strict ``json.loads`` a
    commented ``settings.json`` was silently skipped, and the readable
    ``settings.local.json`` VCO writes in every project then answered
    ``False`` — every ``.py`` Edit was recorded ``no_signal/no_checker``
    although py-compile-check runs."""
    from vco_lib import edit_post_check as epc

    (tmp_path / ".claude").mkdir()
    payload = json.dumps(
        {"hooks": {"PostToolUse": [
            {"matcher": "Edit|Write",
             "hooks": [{"type": "command", "command": _PCC}]}]}},
        indent=2)
    commented = ("// the hook block, kept under version control with notes\n"
                 + payload.replace('"Edit|Write"', '"Edit|Write" /* the shipped pair */'))
    assert "/*" in commented and "//" in commented
    (tmp_path / ".claude" / "settings.json").write_text(commented, encoding="utf-8")
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"A": "1"}}), encoding="utf-8")
    assert epc.registered_for("py_compile", "Edit", str(tmp_path)) is True


def test_a_broken_settings_file_answers_none_even_with_a_readable_local(tmp_path):
    """Review L3: an EXISTING settings file that cannot be parsed keeps the
    table's answer (``None``) — the readable local file must not turn it
    into ``False``."""
    from vco_lib import edit_post_check as epc

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    (tmp_path / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"A": "1"}}), encoding="utf-8")
    assert epc.registered_for("py_compile", "Edit", str(tmp_path)) is None


# ── L10 ───────────────────────────────────────────────────────────────────


def test_the_selection_reuses_the_restore_gate_key_answer(tmp_path):
    from vco_lib import module_gated_delivery as mgd

    src = REPO_ROOT / "templates" / "agents" / mgd.GATEWAY_AGENTS_DIR
    asked: list[str] = []

    def probe(vendor):
        asked.append(vendor)
        return True

    verdict = mgd.GateVerdict(mgd.GateState.DELIVER, "test", "test")
    files, _held = mgd.select_gateway_agents(
        src, verdict, vendor_probe=probe, disabled_vendors=(),
        reach_hint={"kimi": (False, "no 'kimi' vendor key resolved")})
    assert "kimi" not in asked, "the run's restore-gate answer is not asked again"
    assert not any(f.name.startswith("kimi-") for f in files)


def test_an_unreadable_registry_names_the_drop_that_still_works():
    from vco_lib.unregister_collections import plan_drop

    plan = plan_drop("My Project", project_id="id-1", snapshot=None)
    assert plan.targets == [] and plan.kept
    assert "drop-collections --name 'My Project'" in " ".join(plan.notes)


def test_a_skipped_ledger_write_resolves_nothing(tmp_path, monkeypatch):
    from vco_lib import deferral_emit, probe_bound
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    assert deferral_emit.emit(folder, DeferralEntry(
        condition_id="some_condition", title="T", detected="d", why_deferred="w",
        command_to_apply="c", severity="warning"))
    monkeypatch.setattr(probe_bound, "cancelled", lambda: True)
    # present on disk, but the cancelled probe's block writes nothing ...
    assert deferral_emit.resolve_conditions(folder, ["some_condition"]) == 0
    # ... so it is still there, and nothing may claim it was resolved
    assert DeferralReport.read(folder).has_condition("some_condition")


# ── L7: the terminal Remove's preview (dry runs of the two decisions) ──────


def test_the_file_purge_preview_lists_and_changes_nothing(tmp_path):
    from vco_lib import unregister_files

    folder = tmp_path / "proj"
    agent = folder / ".claude" / "agents" / "code-reviewer.md"
    agent.parent.mkdir(parents=True)
    agent.write_bytes(b"shipped\n")
    (folder / ".claude" / "state").mkdir()
    manifest = {"schema_version": 2, "preserved_files": {}, "packs": {},
                "files": {".claude/agents/code-reviewer.md": {"sha256": sha256_bytes(b"shipped\n")}}}
    (folder / ".claude" / ".vco-manifest.json").write_text(json.dumps(manifest))
    (folder / ".claude" / "settings.json").write_text("{}")
    before = sorted(str(p) for p in folder.rglob("*"))
    out = unregister_files.purge_project_files(folder, orchestrator_root=REPO_ROOT, dry_run=True)
    assert sorted(str(p) for p in folder.rglob("*")) == before, "a preview changes nothing"
    assert ".claude/agents/code-reviewer.md" in out["removed"]
    assert ".claude/state" in out["removed"] and ".claude/.vco-manifest.json" in out["removed"]
    # Review L8: the preview computes the strip — a settings.json holding
    # NOTHING of VCO's (`{}` here) gets no generic "VCO's entries" line and
    # is not named as deleted (the real removal leaves it too).
    assert out["settings_removed"] == []
    assert ".claude/settings.json" not in out["removed"]


def test_the_collection_drop_preview_drops_nothing():
    from vco_lib.unregister_collections import drop_project_collections

    calls: list[str] = []
    out = drop_project_collections("Alpha", delete_class=lambda n, **_k: calls.append(n),
                                   dry_run=True)
    assert calls == [] and out["dry_run"] is True
    assert "Alpha_KnowledgeGraph" in out["dropped"]
