# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — the sanctioned per-project scratch dir (``vco_lib.scratch_dir``).

Covers: path creation + perms, name resolution, the TTL knob, the sweep (old
removed, new kept, never leaves the root, never follows a symlink — at the
lane level AND inside a lane), the doctor report, the CLI, the Windows ACL arm
(reusing the tee lib's S5 block), the shipped wrapper pair and the
SessionStart trigger. All fixtures are synthetic and live under ``tmp_path``.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import doctor, scratch_dir
from vco_lib.scratch_dir import ScratchError

REPO = Path(__file__).resolve().parents[1]
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only primitive (mode bits / unprivileged symlinks)")
DAY = 86400.0


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    return proj


def _age(path: Path, seconds: float) -> None:
    """Back-date ``path`` and everything under it (lstat times, no follow)."""
    stamp = time.time() - seconds
    nofollow = os.utime in os.supports_follow_symlinks
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames + filenames:
            entry = os.path.join(dirpath, name)
            if nofollow:
                os.utime(entry, (stamp, stamp), follow_symlinks=False)
            elif not os.path.islink(entry):
                os.utime(entry, (stamp, stamp))
    os.utime(path, (stamp, stamp))


# ---------------------------------------------------------------------------
# Path creation, permissions, naming
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_path_is_created_owner_only_under_the_state_dir(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "lane-a")
    assert lane == proj / ".claude" / "state" / "scratch" / "lane-a"
    assert lane.is_dir()
    for p in (lane, lane.parent):
        assert stat.S_IMODE(p.stat().st_mode) == 0o700, oct(p.stat().st_mode)


@POSIX_ONLY
def test_a_preexisting_loose_dir_is_tightened(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    loose = proj / ".claude" / "state" / "scratch" / "lane-a"
    loose.mkdir(parents=True)
    os.chmod(loose, 0o755)
    os.chmod(loose.parent, 0o755)
    scratch_dir.scratch_path(proj, "lane-a")
    assert stat.S_IMODE(loose.stat().st_mode) == 0o700
    assert stat.S_IMODE(loose.parent.stat().st_mode) == 0o700


def test_default_name_is_the_sanitised_session_id(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    env = {"CLAUDE_CODE_SESSION_ID": "abc-123"}
    assert scratch_dir.scratch_path(proj, env=env).name == "abc-123"
    # Legacy name honoured when the harness one is absent.
    assert scratch_dir.lane_name(env={"CLAUDE_SESSION_ID": "s_9"}) == "s_9"
    # A hostile id never composes a path.
    assert scratch_dir.lane_name(env={"CLAUDE_CODE_SESSION_ID": "../x"}) == "default"
    assert scratch_dir.lane_name(env={}) == "default"


@pytest.mark.parametrize("bad", ["", ".", "..", ".hidden", "a/b", "a\\b", "x" * 129])
def test_an_unsafe_name_is_a_usage_error(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        scratch_dir.scratch_path(_project(tmp_path), bad)


def test_dotted_lane_names_are_allowed(tmp_path: Path) -> None:
    assert scratch_dir.scratch_path(_project(tmp_path), "v0.2.104-scratch").is_dir()


@POSIX_ONLY
def test_a_symlinked_state_dir_is_refused_and_nothing_is_created(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / ".claude" / "state").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ScratchError):
        scratch_dir.scratch_path(proj, "lane")
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# TTL knob
# ---------------------------------------------------------------------------


def test_ttl_env_wins_then_claude_env_then_default(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    assert scratch_dir.resolve_ttl_hours(proj, env={}) == 168
    (proj / ".claude" / "env").write_text("export VCO_SCRATCH_TTL_HOURS=\"24\"\n", encoding="utf-8")
    assert scratch_dir.resolve_ttl_hours(proj, env={}) == 24
    assert scratch_dir.resolve_ttl_hours(proj, env={"VCO_SCRATCH_TTL_HOURS": "0"}) == 0
    for bad in ("abc", "-5", "1.5", ""):
        assert scratch_dir.resolve_ttl_hours(proj, env={"VCO_SCRATCH_TTL_HOURS": bad}) == 168


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def test_sweep_removes_old_lanes_and_keeps_new_ones(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    old = scratch_dir.scratch_path(proj, "old")
    (old / "sub").mkdir()
    (old / "sub" / "big.bin").write_bytes(b"x" * 1000)
    _age(old, 10 * DAY)
    new = scratch_dir.scratch_path(proj, "new")
    (new / "f.txt").write_text("fresh", encoding="utf-8")

    res = scratch_dir.sweep(proj, ttl_hours=168)

    assert res.removed == ["old"] and res.kept == ["new"] and not res.errors
    assert not old.exists() and (new / "f.txt").exists()


def test_a_lane_with_one_fresh_file_deep_inside_is_kept(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "busy")
    (lane / "a" / "b").mkdir(parents=True)
    (lane / "a" / "b" / "old.txt").write_text("x", encoding="utf-8")
    _age(lane, 10 * DAY)
    fresh = lane / "a" / "b" / "fresh.txt"
    fresh.write_text("y", encoding="utf-8")
    old_t = time.time() - 10 * DAY
    os.utime(lane / "a" / "b", (old_t, old_t))  # parent stays old

    res = scratch_dir.sweep(proj, ttl_hours=1)

    assert res.kept == ["busy"] and fresh.exists()


def test_ttl_zero_keeps_everything(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "old")
    _age(lane, 400 * DAY)
    res = scratch_dir.sweep(proj, ttl_hours=0)
    assert res.removed == [] and res.kept == ["old"] and lane.exists()


def test_sweep_reads_the_ttl_knob_when_not_given(tmp_path: Path, monkeypatch) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "two-days")
    _age(lane, 2 * DAY)
    monkeypatch.setenv("VCO_SCRATCH_TTL_HOURS", "72")
    assert scratch_dir.sweep(proj).kept == ["two-days"]
    monkeypatch.setenv("VCO_SCRATCH_TTL_HOURS", "24")
    assert scratch_dir.sweep(proj).removed == ["two-days"]


def test_sweep_without_a_scratch_root_is_a_noop(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    res = scratch_dir.sweep(proj, ttl_hours=1)
    assert res.removed == [] and res.kept == []
    assert not (proj / ".claude" / "state").exists()


def test_sweep_never_touches_siblings_of_the_scratch_root(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "old")
    sibling = proj / ".claude" / "state" / "lean-ctx-tee"
    sibling.mkdir()
    (sibling / "keep.log").write_text("x", encoding="utf-8")
    _age(sibling, 30 * DAY)
    _age(lane, 30 * DAY)

    scratch_dir.sweep(proj, ttl_hours=1)

    assert not lane.exists()
    assert (sibling / "keep.log").exists()


@POSIX_ONLY
def test_symlink_arm_1_a_lane_entry_that_is_a_symlink_is_unlinked_not_followed(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    scratch_dir.scratch_path(proj, "real")
    outside = tmp_path / "outside-precious"
    outside.mkdir()
    (outside / "data.txt").write_text("precious", encoding="utf-8")
    _age(outside, 30 * DAY)  # an old TARGET must not make anything deletable
    link = proj / ".claude" / "state" / "scratch" / "evil"
    link.symlink_to(outside, target_is_directory=True)
    old_t = time.time() - 30 * DAY
    os.utime(link, (old_t, old_t), follow_symlinks=False)

    res = scratch_dir.sweep(proj, ttl_hours=1)

    assert "evil" in res.removed
    assert not os.path.lexists(link)
    assert (outside / "data.txt").read_text(encoding="utf-8") == "precious"


@POSIX_ONLY
def test_symlink_arm_1b_a_fresh_link_to_an_old_target_is_kept(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    outside = tmp_path / "outside-old"
    outside.mkdir()
    _age(outside, 30 * DAY)
    scratch_dir.scratch_path(proj, "x")
    link = proj / ".claude" / "state" / "scratch" / "fresh-link"
    link.symlink_to(outside, target_is_directory=True)
    res = scratch_dir.sweep(proj, ttl_hours=1)
    assert "fresh-link" in res.kept and outside.is_dir()


@POSIX_ONLY
def test_symlink_arm_2_a_symlink_inside_an_old_lane_is_not_descended(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "old")
    outside = tmp_path / "outside-precious"
    outside.mkdir()
    (outside / "data.txt").write_text("precious", encoding="utf-8")
    (lane / "nested").mkdir()
    (lane / "nested" / "to-outside").symlink_to(outside, target_is_directory=True)
    (lane / "file-link").symlink_to(outside / "data.txt")
    _age(lane, 30 * DAY)
    # The target is FRESH: following the link would wrongly keep the lane.
    os.utime(outside / "data.txt")

    res = scratch_dir.sweep(proj, ttl_hours=1)

    assert res.removed == ["old"] and not lane.exists()
    assert (outside / "data.txt").read_text(encoding="utf-8") == "precious"


@POSIX_ONLY
def test_a_symlinked_scratch_root_is_refused_before_any_deletion(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    outside = tmp_path / "outside-root"
    (outside / "victim").mkdir(parents=True)
    (outside / "victim" / "data.txt").write_text("precious", encoding="utf-8")
    _age(outside, 30 * DAY)
    (proj / ".claude" / "state").mkdir()
    (proj / ".claude" / "state" / "scratch").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ScratchError):
        scratch_dir.sweep(proj, ttl_hours=1)
    assert (outside / "victim" / "data.txt").exists()


@POSIX_ONLY
def test_a_lane_swapped_for_a_symlink_after_the_scan_is_left_alone(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "old")
    _age(lane, 30 * DAY)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("precious", encoding="utf-8")
    root = proj / ".claude" / "state" / "scratch"
    info = scratch_dir.LaneInfo("old", "dir", 0.0, 0)
    shutil.rmtree(lane)
    lane.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        scratch_dir._remove_lane(root, info)
    assert (outside / "data.txt").exists()


# ---------------------------------------------------------------------------
# Report + doctor
# ---------------------------------------------------------------------------


def test_report_counts_sizes_and_names_the_oldest(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    a = scratch_dir.scratch_path(proj, "a")
    (a / "f").write_bytes(b"x" * 2048)
    _age(a, 3 * DAY)
    b = scratch_dir.scratch_path(proj, "b")
    (b / "g").write_bytes(b"y" * 1024)

    rep = scratch_dir.report(proj)

    assert rep["count"] == 2 and rep["total_bytes"] == 3072
    assert rep["oldest_name"] == "a" and 71.0 <= rep["oldest_age_hours"] <= 73.0
    assert rep["exists"] is True


def test_doctor_registers_the_probe_full_scope_only() -> None:
    fn, scopes = doctor.PROBES["scratch_dir"]
    assert fn is doctor.probe_scratch_dir
    assert scopes == (doctor.SCOPE_FULL,)


def test_doctor_probe_reports_ok_with_the_numbers(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "lane")
    (lane / "f").write_bytes(b"z" * 10)
    [finding] = doctor.probe_scratch_dir(proj, doctor.DoctorResolvers(), {})
    assert finding.probe == "scratch_dir" and finding.status == doctor.STATUS_OK
    assert finding.detail["count"] == 1 and finding.detail["total_bytes"] == 10
    assert "1 entry" in finding.summary and "lane" in finding.summary


def test_doctor_probe_on_an_empty_project_says_empty(tmp_path: Path) -> None:
    [finding] = doctor.probe_scratch_dir(_project(tmp_path), doctor.DoctorResolvers(), {})
    assert finding.status == doctor.STATUS_OK and "empty" in finding.summary


@POSIX_ONLY
def test_doctor_probe_is_unknown_for_a_redirected_root(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / ".claude" / "state").mkdir()
    (proj / ".claude" / "state" / "scratch").symlink_to(tmp_path, target_is_directory=True)
    [finding] = doctor.probe_scratch_dir(proj, doctor.DoctorResolvers(), {})
    assert finding.status == doctor.STATUS_UNKNOWN


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scratch_dir.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def test_cli_path_prints_and_creates(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    code, out, _ = _cli("path", "--project", str(proj), "--name", "lane-x")
    assert code == 0
    assert Path(out.strip()) == proj / ".claude" / "state" / "scratch" / "lane-x"
    assert Path(out.strip()).is_dir()


def test_cli_bad_name_exits_2(tmp_path: Path) -> None:
    code, out, err = _cli("path", "--project", str(_project(tmp_path)), "--name", "../x")
    assert code == 2 and out == "" and "invalid scratch name" in err


def test_cli_sweep_json_and_report_json(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    lane = scratch_dir.scratch_path(proj, "old")
    _age(lane, 30 * DAY)
    code, out, _ = _cli("sweep", "--project", str(proj), "--ttl-hours", "1", "--json")
    assert code == 0 and json.loads(out)["removed"] == ["old"]
    code, out, _ = _cli("report", "--project", str(proj), "--json")
    assert code == 0 and json.loads(out)["count"] == 0


def test_cli_negative_ttl_is_a_usage_error(tmp_path: Path) -> None:
    code, _, _ = _cli("sweep", "--project", str(_project(tmp_path)), "--ttl-hours", "-1")
    assert code == 2


@POSIX_ONLY
def test_cli_refusal_exits_1(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / ".claude" / "state").mkdir()
    (proj / ".claude" / "state" / "scratch").symlink_to(tmp_path, target_is_directory=True)
    code, _, err = _cli("sweep", "--project", str(proj))
    assert code == 1 and "symlink" in err


def test_cli_without_project_uses_claude_project_dir(tmp_path: Path, monkeypatch) -> None:
    proj = _project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    code, out, _ = _cli("path", "--name", "n")
    assert code == 0 and Path(out.strip()).parent.parent.parent.parent == proj


# ---------------------------------------------------------------------------
# Windows owner-only ACL arm (reuses the tee lib's S5 block)
# ---------------------------------------------------------------------------


def test_windows_arm_refuses_when_the_acl_cannot_be_applied(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(scratch_dir, "_is_windows", lambda: True)
    monkeypatch.setattr(scratch_dir, "ACL_RUNNER", lambda p: calls.append(p) or False)
    with pytest.raises(ScratchError, match="owner-only"):
        scratch_dir.scratch_path(_project(tmp_path), "lane")
    assert calls and calls[0].name == "scratch"


def test_windows_arm_applies_the_acl_to_root_and_lane(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(scratch_dir, "_is_windows", lambda: True)
    monkeypatch.setattr(scratch_dir, "ACL_RUNNER", lambda p: calls.append(p.name) or True)
    lane = scratch_dir.scratch_path(_project(tmp_path), "lane")
    assert lane.is_dir() and calls == ["scratch", "lane"]


def test_the_acl_script_is_the_tee_libs_one_home_block() -> None:
    lib = REPO.joinpath(*scratch_dir.ACL_LIB_REL)
    text = lib.read_text(encoding="utf-8-sig")
    script = scratch_dir._owner_only_acl_script(text)
    begin = text.index(scratch_dir.ACL_BEGIN)
    end = text.index(scratch_dir.ACL_END)
    assert script.startswith(text[begin:end])
    assert "Set-OwnerOnlyAcl $env:VCO_SCRATCH_ACL_PATH -Directory" in script


def test_a_lib_without_the_block_is_a_broken_install() -> None:
    with pytest.raises(ScratchError, match="broken install"):
        scratch_dir._owner_only_acl_script("# nothing here\n")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_the_composed_acl_script_runs_under_pwsh(tmp_path: Path) -> None:
    # On a non-Windows host Set-OwnerOnlyAcl's platform gate returns $true, so
    # this proves the extracted block + trailer parse and exit 0 end-to-end.
    target = tmp_path / "d"
    target.mkdir()
    assert scratch_dir._default_acl_runner(target) is True


# ---------------------------------------------------------------------------
# Shipped wrapper pair + the SessionStart trigger
# ---------------------------------------------------------------------------


def _venv_env(**extra: str) -> dict:
    """``VCT_VENV`` names THIS interpreter directly (a ladder tier on both
    OSes) with the checkout first on ``PYTHONPATH`` — never an installed,
    possibly stale, vco_lib."""
    return _min_env(VCT_VENV=sys.executable, PYTHONPATH=str(REPO), **extra)


def _min_env(**extra: str) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()),
           "WEAVIATE_URL": "http://127.0.0.1:9"}
    env.update(extra)
    return env


@pytest.mark.skipif(sys.platform == "win32", reason="bash .sh leg; Windows runs the .ps1 sibling: tests/test_v02104_scratch_dir.py::test_ps1_wrapper_matches_the_bash_one")
def test_bash_wrapper_prints_the_lane_dir_of_the_project_it_ships_in(tmp_path: Path) -> None:
    from tests.common.wrapper_staging import stage_scripts

    proj = _project(tmp_path)
    scripts = stage_scripts(proj / ".claude" / "scripts", "vco-scratch")
    proc = subprocess.run(
        ["bash", str(scripts / "vco-scratch"), "path", "--name", "w1"],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
        env=_venv_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == (proj / ".claude" / "state" / "scratch" / "w1").resolve()
    # No subcommand defaults to `path` with the session id as the lane.
    proc = subprocess.run(
        ["bash", str(scripts / "vco-scratch")], capture_output=True, text=True, timeout=120,
        env=_venv_env(CLAUDE_CODE_SESSION_ID="sid1"),
    )
    assert proc.returncode == 0 and proc.stdout.strip().endswith("/scratch/sid1"), proc.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_ps1_wrapper_matches_the_bash_one(tmp_path: Path) -> None:
    from tests.common.wrapper_staging import stage_scripts

    proj = _project(tmp_path)
    scripts = stage_scripts(proj / ".claude" / "scripts", "vco-scratch.ps1")
    proc = subprocess.run(
        ["pwsh", *PWSH_BASE_FLAGS, "-File", str(scripts / "vco-scratch.ps1"), "path",
         "--name", "w2"],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path),
        env=_venv_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == (proj / ".claude" / "state" / "scratch" / "w2").resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="bash .sh leg; Windows runs the .ps1 sibling: tests/test_v02104_scratch_dir.py::test_session_start_hook_ps1_sweeps_old_lanes")
def test_session_start_hook_sweeps_old_lanes(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    old = scratch_dir.scratch_path(proj, "old")
    _age(old, 30 * DAY)
    keep = scratch_dir.scratch_path(proj, "new")
    hook = REPO / "templates" / "hooks" / "session-start-kg-loader.sh"
    proc = subprocess.run(
        ["bash", str(hook)], capture_output=True, text=True, timeout=120, input="",
        env=_venv_env(CLAUDE_PROJECT_DIR=str(proj), VCO_SCRATCH_TTL_HOURS="24"),
    )
    assert proc.returncode == 0, proc.stderr
    assert not old.exists() and keep.exists()
    assert "vco-scratch path" in proc.stdout


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_session_start_hook_ps1_sweeps_old_lanes(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    old = scratch_dir.scratch_path(proj, "old")
    _age(old, 30 * DAY)
    keep = scratch_dir.scratch_path(proj, "new")
    hook = REPO / "templates" / "hooks" / "session-start-kg-loader.ps1"
    proc = subprocess.run(
        ["pwsh", *PWSH_BASE_FLAGS, "-File", str(hook)], capture_output=True, text=True,
        timeout=120, input="",
        env=_venv_env(CLAUDE_PROJECT_DIR=str(proj), VCO_SCRATCH_TTL_HOURS="24"),
    )
    assert proc.returncode == 0, proc.stderr
    assert not old.exists() and keep.exists()
    assert "vco-scratch.ps1 path" in proc.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="bash .sh leg; Windows runs the .ps1 sibling: tests/test_v02104_scratch_dir.py::test_session_start_hook_ps1_sweeps_old_lanes")
def test_session_start_hook_leaves_scratch_alone_under_disable_hooks(tmp_path: Path) -> None:
    proj = _project(tmp_path)
    old = scratch_dir.scratch_path(proj, "old")
    _age(old, 30 * DAY)
    hook = REPO / "templates" / "hooks" / "session-start-kg-loader.sh"
    subprocess.run(
        ["bash", str(hook)], capture_output=True, text=True, timeout=120, input="",
        env=_venv_env(CLAUDE_PROJECT_DIR=str(proj), VCO_SCRATCH_TTL_HOURS="24",
                     VCT_DISABLE_HOOKS="1"),
    )
    assert old.exists()


# ---------------------------------------------------------------------------
# Transience: git + code graph (verified, not re-implemented)
# ---------------------------------------------------------------------------


def test_scratch_is_git_excluded_and_transient_to_the_code_graph() -> None:
    from vco_lib.codegraph_row_classify import TRANSIENT_STATE_MARKER
    from vco_lib.git_exclude import VCO_RUNTIME_EXCLUDES

    assert "/.claude/state/" in VCO_RUNTIME_EXCLUDES
    rel = "/".join(scratch_dir.SCRATCH_REL) + "/lane/x.py"
    assert TRANSIENT_STATE_MARKER in rel
