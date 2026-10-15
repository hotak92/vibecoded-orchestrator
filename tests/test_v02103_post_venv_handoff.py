# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — install.py continues AS the venv's python once step 5 filled it.

The defect: ``_ensure_running_under_mcp_venv`` (V45-A) relaunches only when
``.venv`` already exists. On a FRESH install it does not, so step 4 created the
venv, step 5 filled it, and every later in-process step (the KG seed, compose,
the bundle engine via ``sys.executable``) ran on the launching interpreter and
missed the packages step 5 had just installed.

The fix (``vco_lib.install_companions.continue_in_venv``) hands the run off
right after step 5 through the one exec / spawn-and-wait primitive, carrying a
0600 record of what the parent already did: its buffered log lines (a fresh
install has no log file yet), the answers it already has, its deferral
entries. The child restarts ``main()``, skips steps 3/10 and 4/10 by
verification and never asks a question twice.

Red-proofs (each by mutation, restored from a saved copy):
* delete the ``_continue_in_venv_after_deps(...)`` call in ``main()`` →
  ``test_main_hands_off_right_after_step_5_and_the_child_replays`` fails;
* drop ``extra_lines`` from ``load_session`` → the resume tests fail;
* drop ``adopt_carried`` from ``_seed_run_report`` → the carried-entry test fails.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

import install
from vco_lib import install_companions as ic
from vco_lib import install_resume as ir
from vco_lib.deferral_report import DeferralEntry
from vco_lib.install_deferral_flow import InstallDeferralFlow


class _HandedOff(Exception):
    def __init__(self, executable, argv, env):
        super().__init__(executable)
        self.executable, self.argv, self.env = executable, argv, env


def _recording_hand_off(executable, argv, env):
    raise _HandedOff(executable, argv, env)


@pytest.fixture
def clean_record(monkeypatch):
    for key in ic.RELAUNCH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _fake_venv_python(tmp_path: Path) -> Path:
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    return py


# ── continue_in_venv ────────────────────────────────────────────────────────


def test_hands_off_with_a_private_record_and_the_relaunch_stamp(tmp_path, monkeypatch, clean_record):
    py = _fake_venv_python(tmp_path)
    monkeypatch.setattr(ic, "hand_off", _recording_hand_off)
    released: list = []
    state = {"pending_events": ['{"step": "4/10"}'], "decisions": {"storage_choice": {"mode": "named"}},
             "deferral_entries": []}
    with pytest.raises(_HandedOff) as hop:
        ic.continue_in_venv(py, ["install.py", "--yes"], state,
                            before_hand_off=lambda: released.append(1))
    env = hop.value.env
    assert hop.value.executable == str(py)
    assert hop.value.argv == [str(py), "install.py", "--yes"]
    assert released == [1], "the singleton lock is released exactly once, before the hop"
    assert env[ic.ENV_RELAUNCHED] == "1" and env[ic.ENV_RELAUNCH_TOKEN]
    record = Path(env[ic.ENV_POST_VENV_HANDOFF])
    try:
        if os.name != "nt":
            assert stat.S_IMODE(record.stat().st_mode) == 0o600
        data = json.loads(record.read_text(encoding="utf-8"))
        assert data["pending_events"] == state["pending_events"]
        assert data["decisions"] == state["decisions"]
    finally:
        record.unlink(missing_ok=True)
    assert ic.ENV_POST_VENV_HANDOFF not in os.environ, "this process's own env is untouched"


def test_a_handoff_child_never_hands_off_again(tmp_path, monkeypatch, clean_record):
    monkeypatch.setenv(ic.ENV_POST_VENV_HANDOFF, str(tmp_path / "gone.json"))
    monkeypatch.setattr(ic, "hand_off", lambda *a: pytest.fail("loop: the child handed off again"))
    released: list = []
    assert ic.continue_in_venv(_fake_venv_python(tmp_path), ["install.py"], {},
                               before_hand_off=lambda: released.append(1)) is False
    assert released == [], "the lock is kept when nothing hands off"


def test_already_inside_the_venv_is_a_no_op(tmp_path, monkeypatch, clean_record):
    py = Path(sys.prefix) / "bin" / "python"
    monkeypatch.setattr(ic, "hand_off", lambda *a: pytest.fail("handed off from inside the venv"))
    monkeypatch.setattr(ic, "is_running_inside_venv", lambda p, prefix=None: True)
    assert ic.continue_in_venv(py, ["install.py"], {}) is False


def test_a_missing_interpreter_continues_here_and_says_so(tmp_path, monkeypatch, capsys, clean_record):
    monkeypatch.setattr(ic, "hand_off", lambda *a: pytest.fail("handed off to a missing interpreter"))
    released: list = []
    assert ic.continue_in_venv(tmp_path / "nope" / "python", ["install.py"], {},
                               before_hand_off=lambda: released.append(1)) is False
    assert "cannot continue under the venv" in capsys.readouterr().out
    assert released == []


# ── the record ──────────────────────────────────────────────────────────────


def test_the_record_round_trips_once_and_is_deleted(tmp_path):
    path = ic.write_post_venv_handoff({"pending_events": ["a"], "decisions": {"k": 1},
                                       "deferral_entries": [{"condition_id": "x"}]},
                                      tmp_dir=str(tmp_path))
    rec = ic.load_post_venv_handoff({ic.ENV_POST_VENV_HANDOFF: path})
    assert rec.active and rec.pending_events == ["a"] and rec.has("k") and rec.done("k")
    assert rec.replay("k", lambda: pytest.fail("asked again")) == 1
    assert rec.replay("other", lambda: "asked") == "asked"
    assert rec.deferral_entries == [{"condition_id": "x"}]
    assert not Path(path).exists(), "read once, then gone"


def test_no_variable_is_the_empty_record():
    rec = ic.load_post_venv_handoff({})
    assert not rec.active and rec.pending_events == [] and not rec.has("adopt_project_mode")


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"format": 99})])
def test_an_unreadable_record_still_guards_the_loop(tmp_path, capsys, content):
    path = tmp_path / "rec.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    rec = ic.load_post_venv_handoff({ic.ENV_POST_VENV_HANDOFF: str(path)})
    assert rec.active, "the loop guard must hold even without the record"
    assert rec.decisions == {} and rec.pending_events == []
    assert "unreadable" in capsys.readouterr().err


# ── resume from the parent's buffered lines ─────────────────────────────────


def _line(step, phase, data=None, detail=""):
    from datetime import datetime, timezone
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "actor": "install.py",
           "step": step, "phase": phase, "detail": detail}
    if data is not None:
        rec["data"] = data
    return json.dumps(rec)


def test_a_fresh_install_child_resumes_steps_3_and_4_from_the_buffered_lines(tmp_path):
    lines = [_line("1/10", "start"), _line("1/10", "ok", {"version": "3.12.0"}),
             _line("choices", "ok", {"value": "cpu"}, detail="embedding_mode"),
             _line("3/10", "ok", {"venv_python": "x"}), _line("4/10", "ok", {"deps_fingerprint": {}})]
    session = ir.load_session(tmp_path / "absent.jsonl", enabled=True, extra_lines=lines)
    assert session.completed("3/10") and session.completed("4/10")
    assert session.data["4/10"] == {"deps_fingerprint": {}}
    assert session.choices["embedding_mode"] == {"value": "cpu"}
    assert not ir.load_session(tmp_path / "absent.jsonl", enabled=True).completed("4/10")


def test_buffered_lines_read_after_the_file(tmp_path):
    log = tmp_path / "install.jsonl"
    log.write_text(_line("1/10", "start") + "\n" + _line("4/10", "error") + "\n", encoding="utf-8")
    session = ir.load_session(log, enabled=True, extra_lines=[_line("4/10", "ok", {})])
    assert session.completed("4/10"), "the not-yet-written line is the later one"


# ── the run report survives the hop ─────────────────────────────────────────


def _owned_entry(cid="env_secrets_retained_in_plaintext"):
    return DeferralEntry(condition_id=cid, title="t", detected="d", why_deferred="w",
                         command_to_apply="c")


def test_an_owned_entry_from_a_skipped_step_reaches_the_child_ledger(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    parent = InstallDeferralFlow(folder, owned_ids={"env_secrets_retained_in_plaintext"},
                                 owned_prefixes=())
    parent.seed()
    parent.report.add_entry(_owned_entry())
    carried = parent.carried_state()

    child = InstallDeferralFlow(folder, owned_ids={"env_secrets_retained_in_plaintext"},
                                owned_prefixes=())
    child.seed()
    assert child.adopt_carried(carried) == 1
    child.finalize()
    sidecar = (folder / ".claude" / "context").glob("UPDATE_DEFERRED*")
    text = "".join(p.read_text(encoding="utf-8") for p in sidecar)
    assert "env_secrets_retained_in_plaintext" in text


def test_seeded_foreign_entries_are_not_carried(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    seeder = InstallDeferralFlow(folder, owned_ids=(), owned_prefixes=())
    seeder.report.add_entry(_owned_entry("some_foreign_condition"))
    seeder.finalize()
    parent = InstallDeferralFlow(folder, owned_ids=(), owned_prefixes=())
    assert parent.seed() == 1
    assert parent.carried_state() == [], "the child seeds it from disk itself"


# ── install.py: the child replays instead of asking ─────────────────────────


@pytest.fixture
def handoff(monkeypatch):
    def _set(**decisions):
        rec = ic.PostVenvHandoff(active=True, decisions=decisions)
        monkeypatch.setattr(install, "_HANDOFF", rec)
        return rec
    yield _set


def test_the_env_secret_offer_is_not_made_twice(handoff, tmp_path, monkeypatch):
    handoff(env_secret_offer_made=True)
    monkeypatch.setattr(install._secrets_audit, "audit_env_secrets",
                        lambda p: pytest.fail("the .env was audited again"))
    install._audit_and_offer_env_secret_migration(tmp_path, install.DeferralReport(), None,
                                                  type("A", (), {"yes": False})())


def test_the_lean_ctx_offer_is_not_made_twice(handoff, monkeypatch):
    handoff(lean_ctx_offer_made=True)
    monkeypatch.setattr(install, "_find_lean_ctx_binary", lambda: None)
    monkeypatch.setattr(install, "_maybe_install_lean_ctx",
                        lambda args: pytest.fail("lean-ctx was offered again"))
    args = type("A", (), {"quiet": False, "no_lean_ctx": False})()
    install._detect_optional_companions(args)


def test_adopt_pending_events_rebuffers_them_first(monkeypatch, tmp_path):
    path = ic.write_post_venv_handoff({"pending_events": ["parent\n"], "decisions": {}},
                                      tmp_dir=str(tmp_path))
    monkeypatch.setenv(ic.ENV_POST_VENV_HANDOFF, path)
    monkeypatch.setattr(install, "_PENDING_EVENTS", ["child\n"])
    monkeypatch.setattr(install, "_HANDOFF", ic.PostVenvHandoff())
    install._adopt_post_venv_handoff()
    assert install._PENDING_EVENTS == ["parent\n", "child\n"]
    assert install._HANDOFF.active


# ── main(): the hand-off happens right after step 5, and the child replays ──


class _ReachedPostVenv(Exception):
    pass


def _drive_main_to_step_5(monkeypatch, tmp_path, calls):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(install, "PROJECT_ROOT", root)  # no state/logs: the fresh-install shape
    py = _fake_venv_python(tmp_path) if not (tmp_path / "venv").exists() else tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(install, "_ensure_running_under_mcp_venv", lambda: None)
    monkeypatch.setattr(install, "validate_source_repo", lambda r: None)
    monkeypatch.setattr(install, "_resolve_adopt_project_mode",
                        lambda a, install_path=None: calls.append("adopt-prompt") or "adopt")
    monkeypatch.setattr(install, "_audit_and_offer_env_secret_migration",
                        lambda *a: calls.append("secret-offer") if not install._HANDOFF.done(
                            "env_secret_offer_made") else None)
    # the real step 1 logs the session marker resume keys on
    monkeypatch.setattr(install, "_check_python_version",
                        lambda: install._log_install_event("1/10", "start", "python"))
    monkeypatch.setattr(install, "_check_prerequisites", lambda: None)
    monkeypatch.setattr(install, "_detect_system", lambda a, r: object())
    monkeypatch.setattr(install, "_print_system_info", lambda s: None)
    monkeypatch.setattr(install, "_detect_optional_companions", lambda a: None)
    monkeypatch.setattr(install, "_choose_embedding_config", lambda s, a: {"description": "x"})
    monkeypatch.setattr(install, "_finalize_embed_config_for_host", lambda *a, **k: None)
    monkeypatch.setattr(install, "_reconcile_install_active_embedding", lambda *a: None)
    monkeypatch.setattr(install, "_prompt_storage_location",
                        lambda r, a: calls.append("storage-prompt") or {"mode": "named"})
    monkeypatch.setattr(install, "_persist_storage_choice", lambda c, r: calls.append("storage-persist"))

    def _create_venv(_root):
        install._log_install_event("3/10", "ok", "venv", data={"venv_python": str(py)})
        return py

    def _deps(venv_python, dev):
        if install._resume_verified("4/10", "Dependencies", lambda rec: (True, "fake verified")):
            calls.append("deps-skipped")
            return
        calls.append("deps-installed")
        install._log_install_event("4/10", "ok", "deps", data={"deps_fingerprint": {}})

    monkeypatch.setattr(install, "_create_venv", _create_venv)
    monkeypatch.setattr(install, "_install_requirements", _deps)
    monkeypatch.setattr(install, "_run_machine_migrations",
                        lambda r: (_ for _ in ()).throw(_ReachedPostVenv()))
    monkeypatch.setattr(ic, "hand_off", _recording_hand_off)
    monkeypatch.setattr(install, "_PENDING_EVENTS", [])
    monkeypatch.setattr(install, "_HANDOFF", ic.PostVenvHandoff())
    return root


def test_main_hands_off_right_after_step_5_and_the_child_replays(tmp_path, monkeypatch, clean_record):
    calls: list = []
    folder = tmp_path / "ledger"
    folder.mkdir()
    argv = ["install.py", "--yes", "--suppress-lean-ctx-warning", "--no-resume",
            "--project-folder", str(folder)]

    # ── parent: a fresh install on the launching interpreter ──
    _drive_main_to_step_5(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(install.sys, "argv", list(argv))
    with pytest.raises(_HandedOff) as hop:
        install.main()
    assert calls == ["adopt-prompt", "secret-offer", "storage-prompt", "storage-persist",
                     "deps-installed"], "the parent stops at the hand-off, before step 5's successors"
    record = json.loads(Path(hop.value.env[ic.ENV_POST_VENV_HANDOFF]).read_text(encoding="utf-8"))
    assert record["decisions"]["adopt_project_mode"] == "adopt"
    assert record["decisions"]["storage_choice"] == {"mode": "named"}
    assert any('"4/10"' in line for line in record["pending_events"])

    # ── child: the same argv + the hop's token, the hop's env ──
    calls.clear()
    _drive_main_to_step_5(monkeypatch, tmp_path, calls)
    for key, value in hop.value.env.items():
        if key in ic.RELAUNCH_ENV_KEYS:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(ic, "start_parent_watch", lambda: False)
    # the real hand_off appends the token half only the direct child receives
    token = ic.RELAUNCH_TOKEN_ARG + hop.value.env[ic.ENV_RELAUNCH_TOKEN]
    monkeypatch.setattr(install.sys, "argv", [*hop.value.argv[1:], token])
    with pytest.raises(_ReachedPostVenv):
        install.main()
    assert calls == ["deps-skipped"], (
        "the child asks nothing again and resumes step 4 by verification — even under --no-resume")
    assert not Path(hop.value.env[ic.ENV_POST_VENV_HANDOFF]).exists()


def test_seed_run_report_takes_the_parents_entries_as_this_runs_own(tmp_path, monkeypatch):
    """The install.py wiring of ``adopt_carried``: a hand-off child's run
    report starts with what its parent detected before the hop."""
    from vco_lib.deferral_report import _entry_to_dict

    rec = ic.PostVenvHandoff(active=True, deferral_entries=[_entry_to_dict(_owned_entry())])
    monkeypatch.setattr(install, "_HANDOFF", rec)
    flow = InstallDeferralFlow(tmp_path, owned_ids={"env_secrets_retained_in_plaintext"},
                               owned_prefixes=())
    install._seed_run_report(flow)
    assert [e.condition_id for e in flow.report.entries] == ["env_secrets_retained_in_plaintext"]
