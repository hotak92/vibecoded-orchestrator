# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-LOWS (R3 AA-5) — the lanes-dir rmdir check normalises paths
the same way in both siblings.

After the reap removes a lane reservation's worktree, ``_wtrec_rmdir_lanes_parent``
(.sh) and ``Remove-WtrecLanesParent`` (.ps1) remove the now-empty
``<R>.vco-lanes/`` only when the worktree's parent IS that directory. The .ps1
compared lexically normalised paths (``GetFullPath`` + trailing separators
trimmed) while the .sh compared raw strings, so a recorded ``toplevel`` or
``path`` carrying ``..`` or a trailing ``/.`` removed the directory on Windows
but not on POSIX. Both siblings now normalise both paths lexically first.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "templates" / "hooks" / "_lib"
_PS = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the bash arm is POSIX-only")

FLAVOURS = [
    pytest.param("sh", id="sh"),
    pytest.param("ps1", id="ps1", marks=pytest.mark.skipif(_PS is None, reason="no pwsh on PATH")),
]

# (top, path) relative to tmp_path; every case names <tmp>/R.vco-lanes as the parent.
MATCHING = [
    pytest.param("R", "R.vco-lanes/lane", id="plain"),
    pytest.param("R/", "R.vco-lanes/lane", id="top-trailing-slash"),
    pytest.param("R", "R.vco-lanes/lane/", id="path-trailing-slash"),
    pytest.param("x/../R", "R.vco-lanes/lane", id="top-dotdot"),
    pytest.param("R", "R.vco-lanes/sub/../lane", id="path-dotdot"),
    pytest.param("R", "R.vco-lanes/lane/.", id="path-trailing-dot"),
    pytest.param("./R", "R.vco-lanes//lane", id="dot-and-double-slash"),
]

NOT_MATCHING = [
    pytest.param("R", "other/lane", id="other-parent"),
    pytest.param("R", "R.vco-lanes/lane/../../elsewhere/lane", id="dotdot-escapes"),
    pytest.param("x/R", "R.vco-lanes/lane", id="different-top"),
]


def _call(flavour: str, tmp_path: Path, top: str, path: str) -> None:
    t = f"{tmp_path}/{top}"
    p = f"{tmp_path}/{path}"
    if flavour == "sh":
        argv = ["bash", "-c", 'source "$1"; _wtrec_rmdir_lanes_parent "$2" "$3"', "_",
                str(LIB / "worktree-records.sh"), t, p]
    else:
        assert _PS is not None
        wrapper = tmp_path / "call.ps1"
        wrapper.write_text(
            "param([string]$lib, [string]$top, [string]$path)\n"
            ". $lib\nRemove-WtrecLanesParent $top $path\n", encoding="utf-8")
        argv = [_PS, *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(wrapper),
                str(LIB / "worktree-records.ps1"), t, p]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize(("top", "path"), MATCHING)
def test_normalised_parent_removes_empty_lanes_dir(flavour: str, top: str, path: str, tmp_path: Path) -> None:
    (tmp_path / "R").mkdir()
    (tmp_path / "x").mkdir()
    lanes = tmp_path / "R.vco-lanes"
    lanes.mkdir()
    _call(flavour, tmp_path, top, path)
    assert not lanes.exists(), f"{flavour}: {top!r} / {path!r} should name <R>.vco-lanes"


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize(("top", "path"), NOT_MATCHING)
def test_other_parent_leaves_lanes_dir(flavour: str, top: str, path: str, tmp_path: Path) -> None:
    (tmp_path / "R").mkdir()
    (tmp_path / "x").mkdir()
    lanes = tmp_path / "R.vco-lanes"
    lanes.mkdir()
    _call(flavour, tmp_path, top, path)
    assert lanes.is_dir(), f"{flavour}: {top!r} / {path!r} must not touch <R>.vco-lanes"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_non_empty_lanes_dir_survives_normalised_match(flavour: str, tmp_path: Path) -> None:
    (tmp_path / "R").mkdir()
    (tmp_path / "x").mkdir()
    lanes = tmp_path / "R.vco-lanes"
    (lanes / "other-lane").mkdir(parents=True)
    _call(flavour, tmp_path, "x/../R/", "R.vco-lanes/lane/")
    assert (lanes / "other-lane").is_dir()
