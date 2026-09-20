# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The file-backed `.claude/env` reader must agree with `source` (v0.2.95).

THE DEFECT (found 2026-09-16). A project's `.claude/env` is read by TWO
readers with OPPOSITE precedence when ``VCT_ORCHESTRATOR_ROOT`` appears more
than once in the file:

* ``source .claude/env`` — the shell channel every hook uses — LAST
  assignment wins (shell semantics);
* the venv ladder's file-backed reader
  (``_vct_ladder_orchestrator_root_from_project_env`` /
  ``Get-VctLadderOrchestratorRootFromProjectEnv``) — took the FIRST match.

So a user who appended ``VCT_ORCHESTRATOR_ROOT=/new/root`` at the bottom of
the file (the natural way to override) got the NEW root in every hook and
the OLD root in ``kg-sync``'s venv resolution. Duplicates are realistic: the
env writer preserves user-edited lines and a header comment advertises the
key.

WHAT THESE TESTS PIN (both flavours driven through the REAL reader
functions — bash sourced, pwsh dot-sourced when on PATH):

1. LAST assignment wins when the key appears twice.
2. The single-occurrence case still resolves.
3. ``export ``-prefixed and quoted (single and double) forms.
4. A commented-out assignment is ignored even when it is the LAST line.
5. CRLF line endings: the LAST root is returned with no carriage return
   leaked into the value (the bash reader also strips the trailing CR now —
   a Windows hand-edit of the file writes CRLF, and a path with a stray
   ``\\r`` never resolves).
6. A missing file reads as "no root".
7. End-to-end (bash): with BOTH roots qualifying clones, the interpreter a
   staged wrapper actually RUNS comes from the LAST root's venv.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.wrapper_staging import stage_scripts
from tests.test_v0294_wrapper_venv_ladder_parity import _stripped_env

REPO_ROOT = Path(__file__).resolve().parent.parent
LADDER_PS1 = REPO_ROOT / "templates" / "scripts" / "vct_venv_ladder.ps1"

pytestmark = [
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH"),
]


# ── harness: drive the REAL reader functions ───────────────────────────────


def _stage_ladder(tmp_path: Path, env_bytes: bytes | None) -> Path:
    """Stage the ladder the way a bundle installs it (`.claude/scripts/`),
    plus a `.claude/env` when *env_bytes* is not None. Returns the scripts
    dir, which is the argument both readers take."""
    scripts = stage_scripts(tmp_path / ".claude" / "scripts")
    if env_bytes is not None:
        (tmp_path / ".claude" / "env").write_bytes(env_bytes)
    return scripts


def _read_via_bash(scripts_dir: Path) -> str | None:
    """The ladder's real bash reader. None = its explicit no-root answer."""
    proc = subprocess.run(
        [
            "bash", "-c",
            f'. "{scripts_dir}/vct_venv_ladder.sh"; '
            f'_vct_ladder_orchestrator_root_from_project_env "{scripts_dir}"',
        ],
        capture_output=True, env=_stripped_env(), timeout=60,
    )
    if proc.returncode == 1:
        return None
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    out = proc.stdout.decode("utf-8")
    assert "\r" not in out, f"reader leaked a carriage return: {out!r}"
    return out


def _read_via_pwsh(scripts_dir: Path) -> str | None:
    """The ladder's real PowerShell reader, run when pwsh is available."""
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        pytest.skip("pwsh not on PATH — the .ps1 reader is source-gated instead")
    proc = subprocess.run(
        [
            exe, "-NoProfile", "-Command",
            f'. "{scripts_dir}/vct_venv_ladder.ps1"; '
            f'$v = Get-VctLadderOrchestratorRootFromProjectEnv -ScriptDir "{scripts_dir}"; '
            'if ($null -eq $v) { Write-Output "<NO-ROOT>" } '
            'else { Write-Output $v }',
        ],
        capture_output=True, text=True, env=_stripped_env(), timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.rstrip("\n")
    assert "\r" not in out, f"reader leaked a carriage return: {out!r}"
    return None if out == "<NO-ROOT>" else out


_READERS = {"bash": _read_via_bash, "pwsh": _read_via_pwsh}


def _fake_clone_with_speaking_venv(root: Path, tag: str) -> Path:
    """A directory the ladder accepts as an orchestrator clone (the
    `install.py` + `first-install.sh` markers), whose venv `python` PASSES
    the import probe (`-c` exits 0) and NAMES ITS ROOT when run for real —
    so the end-to-end test can tell WHICH root's interpreter executed."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "install.py").write_text("", encoding="utf-8")
    (root / "first-install.sh").write_text("", encoding="utf-8")
    bindir = root / ".venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    py = bindir / "python"
    py.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        f'echo "VENV-CLONE={tag}"\n',
        encoding="utf-8",
    )
    py.chmod(0o755)
    return root


# ── reader-level cases, both flavours ──────────────────────────────────────


@pytest.mark.parametrize("flavour", sorted(_READERS))
def test_last_assignment_wins_when_the_key_appears_twice(
    tmp_path: Path, flavour: str
) -> None:
    """THE defect: old root first, user's appended override last — the LAST
    one is what `source .claude/env` gives every hook, so it is what the
    ladder's file-backed reader must return too."""
    old = tmp_path / "old-clone"
    new = tmp_path / "new-clone"
    scripts = _stage_ladder(
        tmp_path,
        f"VCT_ORCHESTRATOR_ROOT={old}\n"
        f"VCT_ORCHESTRATOR_ROOT={new}\n".encode(),
    )
    assert _READERS[flavour](scripts) == str(new)


@pytest.mark.parametrize("flavour", sorted(_READERS))
def test_single_occurrence_still_resolves(tmp_path: Path, flavour: str) -> None:
    only = tmp_path / "only-clone"
    scripts = _stage_ladder(
        tmp_path, f"VCT_ORCHESTRATOR_ROOT={only}\n".encode(),
    )
    assert _READERS[flavour](scripts) == str(only)


@pytest.mark.parametrize("flavour", sorted(_READERS))
def test_export_prefix_and_both_quote_kinds(
    tmp_path: Path, flavour: str
) -> None:
    """The three line shapes the env writer and hand-edits actually produce:
    `export KEY="v"`, `KEY='v'`, and `export KEY='v'` — LAST one wins."""
    a = tmp_path / "a-clone"
    b = tmp_path / "b-clone"
    c = tmp_path / "c-clone"
    scripts = _stage_ladder(
        tmp_path,
        f'export VCT_ORCHESTRATOR_ROOT="{a}"\n'
        f"VCT_ORCHESTRATOR_ROOT='{b}'\n"
        f"export VCT_ORCHESTRATOR_ROOT='{c}'\n".encode(),
    )
    assert _READERS[flavour](scripts) == str(c)


@pytest.mark.parametrize("flavour", sorted(_READERS))
def test_a_commented_out_assignment_is_ignored_even_when_last(
    tmp_path: Path, flavour: str
) -> None:
    """A `#`-commented line is not an assignment — `source` skips it, so the
    reader must too, keeping the LAST REAL assignment."""
    real = tmp_path / "real-clone"
    scripts = _stage_ladder(
        tmp_path,
        f"VCT_ORCHESTRATOR_ROOT={real}\n"
        f"# VCT_ORCHESTRATOR_ROOT=/commented/clone\n".encode(),
    )
    assert _READERS[flavour](scripts) == str(real)


@pytest.mark.parametrize("flavour", sorted(_READERS))
def test_crlf_line_endings_last_wins_and_no_cr_leaks(
    tmp_path: Path, flavour: str
) -> None:
    """A Windows hand-edit writes CRLF. The LAST root must come back clean —
    a value with a trailing carriage return is a path that never resolves,
    which is not tolerance. (bash only needed the strip added; Get-Content
    already drops the terminator on the pwsh side.)"""
    old = tmp_path / "old-clone"
    new = tmp_path / "new-clone"
    scripts = _stage_ladder(
        tmp_path,
        f"VCT_ORCHESTRATOR_ROOT={old}\r\n"
        f'export VCT_ORCHESTRATOR_ROOT="{new}"\r\n'.encode(),
    )
    assert _READERS[flavour](scripts) == str(new)


@pytest.mark.parametrize("flavour", sorted(_READERS))
def test_missing_env_file_reads_as_no_root(
    tmp_path: Path, flavour: str
) -> None:
    scripts = _stage_ladder(tmp_path, None)
    assert _READERS[flavour](scripts) is None


# ── end-to-end: which venv actually RUNS ───────────────────────────────────


def test_staged_wrapper_runs_the_last_roots_venv(tmp_path: Path) -> None:
    """Both roots are qualifying clones, so EITHER precedence resolves a
    working interpreter — only the identity of the one that RUNS tells them
    apart. It must be the LAST root's: that is the whole disagreement between
    the hook channel (`source`) and kg-sync's venv resolution."""
    old = _fake_clone_with_speaking_venv(tmp_path / "old-clone", "OLD")
    new = _fake_clone_with_speaking_venv(tmp_path / "new-clone", "NEW")

    scripts = stage_scripts(tmp_path / ".claude" / "scripts", "kg-duplicates")
    (tmp_path / ".claude" / "env").write_text(
        f"VCT_ORCHESTRATOR_ROOT={old}\n"
        f"VCT_ORCHESTRATOR_ROOT={new}\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["bash", str(scripts / "kg-duplicates")],
        capture_output=True, text=True, env=_stripped_env(),
        cwd=str(tmp_path), timeout=120,
    )
    assert proc.returncode == 0, f"{proc.stdout!r} {proc.stderr!r}"
    assert "VENV-CLONE=NEW" in proc.stdout, proc.stdout
    assert "VENV-CLONE=OLD" not in proc.stdout, (
        f"the wrapper ran the FIRST root's venv — the reader and `source` "
        f"still disagree: {proc.stdout!r}"
    )


# ── source gate for the .ps1 (runs where pwsh is absent) ──────────────────


def test_ps1_reader_scans_the_whole_file() -> None:
    """Parse gate, in the family style of the v0294 parity suite: the .ps1
    reader must WALK the whole file — a `return` inside the scan loop is
    first-match-wins restored by construction."""
    text = LADDER_PS1.read_text(encoding="utf-8-sig")
    start = text.index("function Get-VctLadderOrchestratorRootFromProjectEnv")
    end = text.index("\n}", start)
    body = text[start:end]
    loop = body[body.index("foreach") : body.index("if ($null -ne $last)")]
    assert "return" not in loop, (
        "the .ps1 reader returns from inside the scan loop — first "
        "assignment wins again"
    )
    assert "$last = $Matches[2].Trim()" in loop
