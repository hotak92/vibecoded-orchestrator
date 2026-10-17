"""v0.2.104 R6 F5/F6: the "is this path inside the project" helpers of
``_lib/project-root.{sh,ps1}`` (``vco_path_under_root`` /
``Test-VcoPathUnderRoot``) give the SAME answer, and that answer is the
resolved one.

Findings: the .sh leg needed the path's parent directory to exist, so through a
symlinked root a new file in a new directory was "outside" while the .ps1 said
"inside"; the .ps1 accepted ``\\`` as a separator on Linux; and both had a raw
string-prefix fast path that accepted ``root/../outside`` and a symlink leaving
the root. Now both reject ``.``/``..`` segments (and, off Windows, a backslash),
resolve symlinks through the existing part of each path (missing tail kept),
and compare the resolved forms only.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "templates" / "hooks" / "_lib"
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash") or "bash"

def _tree(tmp_path: Path) -> Path:
    """Symlinks need privileges on some Windows hosts: no fixture, no test."""
    base = tmp_path / "e1"
    (base / "proj" / "sub").mkdir(parents=True)
    (base / "proj" / "sub" / "a.py").write_text("x\n")
    (base / "outside").mkdir()
    (base / "outside" / "o.py").write_text("x\n")
    try:
        (base / "alias").symlink_to(base / "proj", target_is_directory=True)
        (base / "proj" / "escape").symlink_to(base / "outside", target_is_directory=True)
        (base / "proj" / "leaf.py").symlink_to(base / "outside" / "o.py")
        (base / "proj" / "inleaf.py").symlink_to(base / "proj" / "sub" / "a.py")
    except OSError as exc:
        pytest.skip(f"cannot create symlinks here: {exc}")
    return base


# (root, path, expected) relative to the fixture base.
CASES = [
    ("proj", "proj/sub/a.py", True),
    ("proj", "proj/sub/newdir/new.py", True),
    ("proj", "alias/sub/newdir/new.py", True),       # F5: symlinked path, new dir
    ("alias", "proj/sub/newdir/new.py", True),       # symlinked root, new dir
    ("alias", "alias/sub/a.py", True),
    ("proj", "proj/inleaf.py", True),                # symlinked leaf staying inside
    ("proj", "proj", False),                         # equal is not under
    ("proj", "outside/o.py", False),
    ("proj", "proj/../outside/a.py", False),         # F6: lexical escape
    ("proj", "proj/./sub/a.py", False),              # `.` segments are refused too
    ("proj", "proj/escape/a.py", False),             # F6: symlinked dir leaving root
    ("proj", "proj/escape/new/n.py", False),
    ("proj", "proj/leaf.py", False),                 # symlinked leaf leaving root
    # F5: `\` is a separator on Windows only.
    ("proj", "proj\\sub\\a.py", os.name == "nt"),
    ("proj", "proj/sub/a.py/x", True),               # below a file: lexical tail
    ("missing", "missing/x.py", True),               # both sides missing: lexical
]


def _sh(base: Path, root: str, path: str) -> bool:
    script = (f'. "{LIB / "project-root.sh"}"\n'
              'vco_path_under_root "$1" "$2"')
    res = subprocess.run([BASH, "-c", script, "probe", str(base / root), f"{base}/{path}"],
                         capture_output=True, text=True, timeout=60, env=child_env())
    assert res.returncode in (0, 1), res.stderr
    return res.returncode == 0


def _ps1(base: Path, rows: list[tuple[str, str, bool]]) -> list[bool]:
    assert PWSH is not None
    lines = [f'. "{LIB / "project-root.ps1"}"']
    for root, path, _ in rows:
        r = str(base / root).replace("'", "''")
        p = f"{base}/{path}".replace("'", "''")
        lines.append(f"if (Test-VcoPathUnderRoot -Root '{r}' -Path '{p}') {{ 'IN' }} else {{ 'OUT' }}")
    res = subprocess.run([PWSH, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command", "\n".join(lines)],
                         capture_output=True, text=True, timeout=120, env=child_env())
    assert res.returncode == 0, res.stderr
    return [line.strip() == "IN" for line in res.stdout.strip().splitlines()]


@pytest.mark.skipif(sys.platform == "win32", reason="bash .sh leg; Windows runs the .ps1 sibling: "
                    "tests/test_v02104_path_containment_parity.py::test_ps1_containment_matches_the_same_table")
@pytest.mark.parametrize("root,path,expected", CASES, ids=[f"{r}|{p}" for r, p, _ in CASES])
def test_sh_containment(tmp_path: Path, root: str, path: str, expected: bool) -> None:
    assert _sh(_tree(tmp_path), root, path) is expected


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
def test_ps1_containment_matches_the_same_table(tmp_path: Path) -> None:
    base = _tree(tmp_path)
    got = _ps1(base, CASES)
    assert len(got) == len(CASES)
    mismatches = [(r, p, e) for (r, p, e), g in zip(CASES, got) if g is not e]
    assert not mismatches, mismatches
