# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-W: the project-root rule agrees across every implementation.

The rule's single source of truth is ``vco_lib/project_root.py``. Three
C-rule mirrors exist because their callers cannot import vco_lib:

* ``templates/hooks/_lib/project-root.sh``  — sourced by the bash hooks;
* ``templates/hooks/_lib/project-root.ps1`` — dot-sourced by the .ps1 hooks;
* ``templates/scripts/agent-skill-keyword-match.py::_project_root`` — runs
  under the SYSTEM python3 from a hook, stdlib only.

Each is executed for real (a child process standing in the fixture's start
directory, with a fixture HOME) over ONE shared fixture table, and must give
the SSOT's answer. Change the rule in one place and this goes red until the
others move with it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_LIB = REPO_ROOT / "templates" / "hooks" / "_lib" / "project-root.sh"
PS1_LIB = REPO_ROOT / "templates" / "hooks" / "_lib" / "project-root.ps1"
KW_MATCH = REPO_ROOT / "templates" / "scripts" / "agent-skill-keyword-match.py"
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash")


@dataclass(frozen=True)
class Case:
    """One fixture. Paths are relative to the case's sandbox root."""

    name: str
    settings: tuple = ()          # dirs that get .claude/settings.json
    envfile: tuple = ()           # dirs that get .claude/env
    bare: tuple = ()              # dirs that get a bare .claude/state (no marker)
    git: tuple = ()               # dirs that get a .git/ directory
    start: str = "proj/a/b"       # cwd of the child
    home: str = "home"            # $HOME (the OS home)
    override: Optional[str] = None  # VCT_USER_HOME_OVERRIDE, sandbox-relative
    env: Optional[str] = None     # CLAUDE_PROJECT_DIR, LITERAL (not resolved)
    expected: Optional[str] = None  # sandbox-relative root, "" = none, or env literal
    literal: bool = False         # expected is a literal string (the env rung)
    extra_dirs: tuple = field(default_factory=tuple)


CASES = (
    Case("subfolder_walks_to_settings_root", settings=("proj",), expected="proj"),
    Case("start_is_the_root", settings=("proj",), start="proj", expected="proj"),
    Case("claude_env_file_marks_a_root", envfile=("proj",), expected="proj"),
    Case("stray_bare_claude_is_walked_through", settings=("proj",),
         bare=("proj/a/b", "proj/a"), expected="proj"),
    Case("lone_bare_claude_is_no_root", bare=("proj/a/b",), expected=""),
    Case("nested_git_is_not_a_boundary", settings=("proj",), git=("proj/a",),
         expected="proj"),
    Case("nearest_marked_project_wins", settings=("proj", "proj/a"),
         expected="proj/a"),
    Case("no_marker_anywhere", expected=""),
    Case("home_is_never_a_root", settings=("home",), start="home/x/y",
         expected=""),
    Case("project_under_home_resolves", settings=("home/proj",),
         start="home/proj/a", expected="home/proj"),
    Case("override_home_is_a_boundary", settings=("ovr",), start="ovr/x",
         override="ovr", expected=""),
    Case("os_home_stays_a_boundary_under_override", settings=("home",),
         start="home/x", override="ovr", extra_dirs=("ovr",), expected=""),
    Case("env_wins_over_the_walk", settings=("proj",), env="/the/workspace",
         expected="/the/workspace", literal=True),
    Case("env_is_trimmed", settings=("proj",), env="  /the/workspace  ",
         expected="/the/workspace", literal=True),
    Case("blank_env_is_unset", settings=("proj",), env="   ", expected="proj"),
)


def _build(sandbox: Path, case: Case) -> None:
    for d in (case.start, case.home, *case.extra_dirs):
        (sandbox / d).mkdir(parents=True, exist_ok=True)
    for d in case.settings:
        (sandbox / d / ".claude").mkdir(parents=True, exist_ok=True)
        (sandbox / d / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    for d in case.envfile:
        (sandbox / d / ".claude").mkdir(parents=True, exist_ok=True)
        (sandbox / d / ".claude" / "env").write_text("", encoding="utf-8")
    for d in case.bare:
        (sandbox / d / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    for d in case.git:
        (sandbox / d / ".git").mkdir(parents=True, exist_ok=True)
    if case.override:
        (sandbox / case.override).mkdir(parents=True, exist_ok=True)


def _env(sandbox: Path, case: Case) -> dict:
    base = {
        k: v for k, v in os.environ.items()
        if k not in ("CLAUDE_PROJECT_DIR", "VCT_USER_HOME_OVERRIDE", "HOME",
                     "USERPROFILE", "KG_BASE_DIR")
    }
    base["HOME"] = str(sandbox / case.home)
    if case.override:
        base["VCT_USER_HOME_OVERRIDE"] = str(sandbox / case.override)
    if case.env is not None:
        base["CLAUDE_PROJECT_DIR"] = case.env
    return child_env(base)


def _run(argv: list, sandbox: Path, case: Case) -> str:
    proc = subprocess.run(
        argv, cwd=sandbox / case.start, env=_env(sandbox, case),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip("\r\n")


def _py_ssot(sandbox: Path, case: Case) -> str:
    code = (
        "from vco_lib.project_root import resolve_project_root as r\n"
        "x = r()\nprint('' if x is None else x)\n"
    )
    return _run([sys.executable, "-c", code], sandbox, case)


def _kw_mirror(sandbox: Path, case: Case) -> str:
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('kwm', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "x = m._project_root()\nprint('' if x is None else x)\n"
    )
    return _run([sys.executable, "-c", code, str(KW_MATCH)], sandbox, case)


def _sh_mirror(sandbox: Path, case: Case) -> str:
    script = f'. "{SH_LIB}"; vco_project_root || true; echo'
    return _run([BASH, "-c", script], sandbox, case)


def _ps1_mirror(sandbox: Path, case: Case) -> str:
    script = f". '{PS1_LIB}'; $r = Get-VcoProjectRoot; [Console]::Out.WriteLine($r)"
    return _run([PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", script], sandbox, case)


IMPLS = {
    "py_ssot": (_py_ssot, None),
    "keyword_match_mirror": (_kw_mirror, None),
    "sh": (_sh_mirror, "bash" if BASH is None else None),
    "ps1": (_ps1_mirror, "pwsh" if PWSH is None else None),
}


def _norm(out: str) -> str:
    return os.path.realpath(out) if out else ""


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
@pytest.mark.parametrize("impl", sorted(IMPLS))
def test_every_implementation_gives_the_ssot_answer(impl, case, tmp_path):
    fn, missing = IMPLS[impl]
    if missing:
        pytest.skip(f"{missing} not on PATH")
    sandbox = tmp_path / "sb"
    _build(sandbox, case)
    got = fn(sandbox, case)
    if case.literal:
        assert got == case.expected
        return
    want = _norm(str(sandbox / case.expected)) if case.expected else ""
    assert _norm(got) == want, f"{impl} on {case.name}: got {got!r}"


def test_the_table_agrees_with_the_ssot_in_process(tmp_path, monkeypatch):
    """The expected column itself is the SSOT's answer (no stale table)."""
    from vco_lib.project_root import resolve_project_root

    for case in CASES:
        sandbox = tmp_path / case.name
        _build(sandbox, case)
        monkeypatch.setenv("HOME", str(sandbox / case.home))
        monkeypatch.delenv("USERPROFILE", raising=False)
        if case.override:
            monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(sandbox / case.override))
        else:
            monkeypatch.delenv("VCT_USER_HOME_OVERRIDE", raising=False)
        if case.env is not None:
            monkeypatch.setenv("CLAUDE_PROJECT_DIR", case.env)
        else:
            monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.chdir(sandbox / case.start)
        got = resolve_project_root()
        if case.literal:
            assert str(got) == case.expected, case.name
        elif case.expected:
            assert got is not None and _norm(str(got)) == _norm(str(sandbox / case.expected)), case.name
        else:
            assert got is None, case.name


def test_every_mirror_names_its_ssot():
    """The C-rule mirrors carry the "must match" pointer to the SSOT."""
    for path in (SH_LIB, PS1_LIB, KW_MATCH):
        text = path.read_text(encoding="utf-8")
        assert "vco_lib/project_root.py" in text, path
        assert "MUST MATCH" in text, path


@pytest.mark.skipif(PWSH is None, reason="pwsh not on PATH")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("via", ["start", "home"])
def test_ps1_resolves_symlinks_like_the_ssot(via, tmp_path):
    """A symlink above the project must not hide its marker from the .ps1 walk.

    ``via=start``: the start dir is given through a link (``link -> real``);
    Python's Path.resolve() finds ``real/proj``, and so must the .ps1 mirror.
    ``via=home``: HOME is given through a link to the real home that holds the
    project's parent — the home boundary must be compared on real paths, so the
    walk from inside the real home still stops there.
    """
    sb = tmp_path / "sb"
    (sb / "real" / "proj" / ".claude").mkdir(parents=True)
    (sb / "real" / "proj" / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (sb / "real" / "proj" / "a").mkdir()
    (sb / "link").symlink_to(sb / "real", target_is_directory=True)
    if via == "start":
        home, start, want = sb / "home", sb / "link" / "proj" / "a", sb / "real" / "proj"
        home.mkdir()
    else:
        # HOME spelled through the link names the project dir itself, so the
        # project IS home and must not be accepted as a root.
        home, start, want = sb / "link" / "proj", sb / "real" / "proj" / "a", None
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "VCT_USER_HOME_OVERRIDE", "HOME", "USERPROFILE")}
    env["HOME"] = str(home)
    code = ("import sys\nfrom vco_lib.project_root import find_vco_project_root as f\n"
            "x = f(sys.argv[1])\nprint('' if x is None else x)\n")
    py = subprocess.run([sys.executable, "-c", code, str(start)], env=child_env(env),
                        capture_output=True, text=True, timeout=60, cwd=sb)
    assert py.returncode == 0, py.stderr
    script = (f". '{PS1_LIB}'; $r = Find-VcoProjectRoot -Start '{start}'; "
              "[Console]::Out.WriteLine($r)")
    ps = subprocess.run([PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", script],
                        env=child_env(env), capture_output=True, text=True, timeout=60, cwd=sb)
    assert ps.returncode == 0, ps.stderr
    expected = "" if want is None else os.path.realpath(want)
    assert py.stdout.strip() == expected
    assert ps.stdout.strip() == expected, f"ps1 got {ps.stdout.strip()!r}"
