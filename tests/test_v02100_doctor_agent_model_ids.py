# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 F-W3-05: ``vco doctor`` checks the model ids agent definitions name.

The check itself is :func:`vco_lib.module_gated_delivery.check_agent_model_ids`
(the router's own validation, one home). These tests pin that the doctor's
full pass RUNS it over the directories Claude Code reads — a project's
``.claude/agents`` and the user's ``~/.claude/agents`` — and reports a mistyped
gateway id with the closest valid one, while a valid set, a first-party id and
an unimportable registry each produce the matching non-problem verdict.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vco_lib import doctor
from vco_lib import module_gated_delivery as mgd


@pytest.fixture(autouse=True)
def _own_user_claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest redirects ~/.claude once per SESSION; each test here describes
    # its own user-level definitions, so give it its own directory.
    monkeypatch.setenv("VCT_CLAUDE_DIR", str(tmp_path / "user_claude"))


def _agent(dir_: Path, name: str, model: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{name}.md"
    path.write_text(f"---\nname: {name}\nmodel: {model}\n---\nbody\n", encoding="utf-8")
    return path


def _only(report: doctor.DoctorReport) -> doctor.Finding:
    found = [f for f in report.findings if f.probe == "agent_model_ids"]
    assert len(found) == 1, found
    return found[0]


def test_the_probe_is_registered_full_scope_only() -> None:
    fn, scopes = doctor.PROBES["agent_model_ids"]
    assert fn is doctor.probe_agent_model_ids
    assert scopes == (doctor.SCOPE_FULL,)


def test_a_mistyped_project_agent_id_is_a_problem_naming_the_fix(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    bad = _agent(project / ".claude" / "agents", "qwen-coder", "claude-gw/qwen3.8-max")
    _agent(project / ".claude" / "agents", "fine", "claude-gw/qwen/qwen3.8-max[1m]")
    _agent(project / ".claude" / "agents", "first-party", "opus")

    report = doctor.run_doctor(project, scope=doctor.SCOPE_FULL)
    finding = _only(report)

    assert finding.status == doctor.STATUS_PROBLEM
    assert "claude-gw/qwen3.8-max" in finding.summary
    assert "claude-gw/qwen/qwen3.8-max" in finding.summary
    assert str(bad) in finding.command
    problems = finding.detail["agent_id_problems"]
    assert [Path(p["path"]).name for p in problems] == ["qwen-coder.md"]


def test_the_user_agents_dir_is_checked_too(tmp_path: Path) -> None:
    # The user-level definitions are the hand-written ones F-W1-11a was about.
    user_agents = mgd.agent_definition_dirs(None)[0]
    _agent(user_agents, "deepseek-r", "claude-gw/deepseek-4.1-flash[1m]")
    project = tmp_path / "proj"
    project.mkdir()

    finding = _only(doctor.run_doctor(project, scope=doctor.SCOPE_FULL))

    assert finding.status == doctor.STATUS_PROBLEM
    assert "claude-gw/qwen/deepseek-v4.1-flash[1m]" in finding.summary


def test_valid_and_first_party_ids_leave_the_probe_ok(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _agent(project / ".claude" / "agents", "fine", "claude-gw/qwen/qwen3.8-max[1m]")
    _agent(project / ".claude" / "agents", "first-party", "opus")

    finding = _only(doctor.run_doctor(project, scope=doctor.SCOPE_FULL))

    assert finding.status == doctor.STATUS_OK
    assert not finding.is_problem


def test_no_registry_is_unknown_never_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_router(_dirs):
        raise ImportError("no module named model_router")

    monkeypatch.setattr(mgd, "check_agent_model_ids", _no_router)
    finding = _only(doctor.run_doctor(tmp_path, scope=doctor.SCOPE_FULL))
    assert finding.status == doctor.STATUS_UNKNOWN


def test_the_boot_scope_never_runs_it(tmp_path: Path) -> None:
    _agent(tmp_path / ".claude" / "agents", "qwen-coder", "claude-gw/qwen3.8-max")
    report = doctor.run_doctor(tmp_path, scope=doctor.SCOPE_BOOT)
    assert not [f for f in report.findings if f.probe == "agent_model_ids"]
