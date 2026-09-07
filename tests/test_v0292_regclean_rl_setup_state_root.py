# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 regclean item 1 — `rl_client_setup.{sh,ps1}` stopped writing to ~/.claude.

## What this pins, and why it is a shell test rather than a Python one

`templates/scripts/rl_client_setup.sh` did::

    LOCAL_RL_DATA_DIR="${HOME}/.claude/retrieval_rl_data"
    mkdir -p "${LOCAL_RL_DATA_DIR}"

and `vco_lib/project_init.py::install_project_bundle` runs it through
`_run_rl_client_setup(folder)` **unconditionally** on every project install and
every `--update` (only `--dry-run` skips it). So this pair — not the Python
logger's default, which nothing in the tree constructs — was the LIVE creator
of `~/.claude/retrieval_rl_data` on every user's machine. Register item 28's
Python half was fixed first; a lane then reported this script had "no caller",
which was false. This is the other half.

The standing directive (2026-08-29): **VCO writes NOTHING under `~/.claude`
except what the harness itself requires.** The corpus home is now
`<vct_root>/retrieval_rl_data`, matching
`rl_client.rl_logger.default_rl_data_dir()`.

## Class-C mirror, so: a parity test, executed

The two-line state-root rule now exists in Python (`vco_lib.paths.vct_root_dir`,
the SSOT), bash and PowerShell. CLAUDE.md's A>B>C rule permits that ONLY with an
enforcing parity test, because tier A (spawn the Python) is not available to an
install-time step that must work before a venv resolves. This file EXECUTES all
three and compares, exactly as `test_v0292_wp8_metrics_shell_parity.py` does for
the metrics pair.

Tri-OS (R12/R14): bash runs natively on the Linux runner; the PowerShell half
runs wherever `pwsh`/`powershell` exists (the same runtime Windows and macOS
use, so executing it here exercises the Windows code path's logic); and the
static half — which reads both files and pins the absence of a `~/.claude`
literal — is ALWAYS on, including on a runner with no PowerShell.

**Nothing here touches the real home.** Every invocation gets `$HOME`,
`$USERPROFILE` and `$VCT_STATE_DIR` pointed inside `tmp_path`, and several
assertions are specifically "the fake `~/.claude` was NOT created".
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SH = _REPO_ROOT / "templates" / "scripts" / "rl_client_setup.sh"
_PS1 = _REPO_ROOT / "templates" / "scripts" / "rl_client_setup.ps1"

_PS = shutil.which("pwsh") or shutil.which("powershell")
needs_ps = pytest.mark.skipif(
    _PS is None,
    reason="no pwsh/powershell on PATH; the static + bash halves still guard drift",
)

#: The subdirectory both flavours and the Python logger must agree on.
CORPUS_DIRNAME = "retrieval_rl_data"


def _sandbox(tmp_path: Path, name: str) -> "tuple[Path, Path, Path]":
    """Build one isolated (project, fake_home, state_root) triple."""
    root = tmp_path / name
    project = root / "project"
    fake_home = root / "home"
    state = root / "state"
    project.mkdir(parents=True)
    fake_home.mkdir(parents=True)
    return project, fake_home, state


def _child_env(fake_home: Path, state: "Path | None") -> dict:
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    if state is None:
        env.pop("VCT_STATE_DIR", None)
    else:
        env["VCT_STATE_DIR"] = str(state)
    return env


def _run_sh(project: Path, fake_home: Path, state: "Path | None") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_SH)],
        cwd=str(project),
        capture_output=True,
        text=True,
        env=_child_env(fake_home, state),
        timeout=120,
    )


def _run_ps(project: Path, fake_home: Path, state: "Path | None") -> subprocess.CompletedProcess:
    assert _PS is not None
    return subprocess.run(
        [_PS, "-NoProfile", "-File", str(_PS1)],
        cwd=str(project),
        capture_output=True,
        text=True,
        env=_child_env(fake_home, state),
        timeout=180,
    )


def _ask_python(fake_home: Path, state: "Path | None") -> Path:
    """The SSOT's answer: ``vct_root_dir() / "retrieval_rl_data"``."""
    env = _child_env(fake_home, state)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    code = (
        "import json;"
        "from vco_lib.paths import vct_root_dir;"
        "print(json.dumps(str(vct_root_dir() / 'retrieval_rl_data')))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return Path(json.loads(proc.stdout.strip().splitlines()[-1]))


# --------------------------------------------------------------------------- #
# 1. The leak itself: no write under ~/.claude, ever
# --------------------------------------------------------------------------- #


def test_bash_creates_the_corpus_under_the_state_root_not_claude(tmp_path):
    """Default resolution (no ``$VCT_STATE_DIR``): ``<home>/.vct``, not ``.claude``."""
    project, fake_home, _ = _sandbox(tmp_path, "default")
    proc = _run_sh(project, fake_home, None)

    assert proc.returncode == 0, proc.stderr
    assert (fake_home / ".vct" / CORPUS_DIRNAME).is_dir(), proc.stdout + proc.stderr
    assert not (fake_home / ".claude").exists(), (
        "the script created something under the fake ~/.claude — this is the "
        "leak the fix closes, and it is the exact shape that put "
        "retrieval_rl_data on every user's real home"
    )


def test_bash_honours_vct_state_dir(tmp_path):
    project, fake_home, state = _sandbox(tmp_path, "stateenv")
    proc = _run_sh(project, fake_home, state)

    assert proc.returncode == 0, proc.stderr
    assert (state / CORPUS_DIRNAME).is_dir()
    # Neither fallback root is touched when the override is set.
    assert not (fake_home / ".vct").exists()
    assert not (fake_home / ".claude").exists()


def test_bash_still_creates_the_per_project_dir(tmp_path):
    """The other half of the script's job is unchanged by the move."""
    project, fake_home, state = _sandbox(tmp_path, "projdir")
    proc = _run_sh(project, fake_home, state)

    assert proc.returncode == 0, proc.stderr
    assert (project / ".claude" / "rl-data").is_dir()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits")
def test_bash_keeps_the_dirs_owner_only(tmp_path):
    """chmod 700 survived the rewrite — these logs may hold query embeddings."""
    project, fake_home, state = _sandbox(tmp_path, "modebits")
    _run_sh(project, fake_home, state)

    for d in ((state / CORPUS_DIRNAME), (project / ".claude" / "rl-data")):
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode == 0o700, f"{d} is {oct(mode)}, expected 0o700"


def test_bash_declines_rather_than_guessing_when_no_root_resolves(tmp_path):
    """No ``$VCT_STATE_DIR``, no ``$HOME``, no ``$USERPROFILE`` ⇒ no corpus dir.

    The leave-alone case. A machine that cannot tell us where its state root is
    gets NOTHING created rather than a directory under a guessed path (``/.vct``
    or a relative ``.vct`` under the project) — and the install step must still
    succeed, because this script is a convenience, not a gate.
    """
    project, fake_home, _ = _sandbox(tmp_path, "norootatall")
    env = dict(os.environ)
    env.pop("HOME", None)
    env.pop("USERPROFILE", None)
    env.pop("VCT_STATE_DIR", None)
    proc = subprocess.run(
        ["bash", str(_SH)], cwd=str(project), capture_output=True, text=True,
        env=env, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert (project / ".claude" / "rl-data").is_dir(), "project dir is still owed"
    assert not (project / ".vct").exists()
    assert not (fake_home / ".vct").exists()
    assert not (fake_home / ".claude").exists()


# --------------------------------------------------------------------------- #
# 2. Parity with the Python SSOT, and between the two shells
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("use_state_env", [False, True])
def test_bash_matches_the_python_ssot(tmp_path, use_state_env):
    project, fake_home, state = _sandbox(tmp_path, f"ssot{int(use_state_env)}")
    chosen = state if use_state_env else None
    expected = _ask_python(fake_home, chosen)

    proc = _run_sh(project, fake_home, chosen)
    assert proc.returncode == 0, proc.stderr
    assert expected.is_dir(), (
        f"the shell did not create the directory the Python SSOT names "
        f"({expected}); the two-line rule has drifted"
    )


@needs_ps
@pytest.mark.parametrize("use_state_env", [False, True])
def test_powershell_matches_the_python_ssot(tmp_path, use_state_env):
    project, fake_home, state = _sandbox(tmp_path, f"psssot{int(use_state_env)}")
    chosen = state if use_state_env else None
    expected = _ask_python(fake_home, chosen)

    proc = _run_ps(project, fake_home, chosen)
    assert proc.returncode == 0, proc.stderr
    assert expected.is_dir(), (
        f"the .ps1 did not create {expected} — the sibling has drifted from "
        f"the .sh and from vco_lib.paths.vct_root_dir()"
    )
    assert not (fake_home / ".claude").exists()


@needs_ps
@pytest.mark.parametrize("use_state_env", [False, True])
def test_the_two_shell_flavours_agree(tmp_path, use_state_env):
    """Lockstep, stated directly rather than by transitivity.

    A `.sh`/`.ps1` pair drifting apart is the single richest source of defects
    in this repo, and it drifts silently because CI's POSIX legs never run the
    `.ps1`.
    """
    sh_project, sh_home, sh_state = _sandbox(tmp_path, f"lockstep_sh{int(use_state_env)}")
    ps_project, ps_home, ps_state = _sandbox(tmp_path, f"lockstep_ps{int(use_state_env)}")

    assert _run_sh(sh_project, sh_home, sh_state if use_state_env else None).returncode == 0
    assert _run_ps(ps_project, ps_home, ps_state if use_state_env else None).returncode == 0

    def created(home: Path, state: Path) -> set:
        base = state if use_state_env else home / ".vct"
        return {
            ("corpus", (base / CORPUS_DIRNAME).is_dir()),
            ("claude_home", (home / ".claude").exists()),
        }

    assert created(sh_home, sh_state) == created(ps_home, ps_state)
    assert (ps_project / ".claude" / "rl-data").is_dir()
    assert (sh_project / ".claude" / "rl-data").is_dir()


# --------------------------------------------------------------------------- #
# 3. Static: the literal cannot come back
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("script", [_SH, _PS1], ids=["sh", "ps1"])
def test_no_claude_corpus_literal_survives_in_either_flavour(script):
    """The banned shape, in every spelling either shell could use.

    Comments in these files DISCUSS `~/.claude/retrieval_rl_data` at length —
    deliberately, since the archive's existence is the reason the corpus was
    not moved — so the scan looks only at CODE lines. A comment that explains
    the history is documentation; an expansion in code is the leak.
    """
    lines = script.read_text(encoding="utf-8").splitlines()
    code = [ln for ln in lines if not ln.lstrip().startswith("#")]
    joined = "\n".join(code)

    for banned in (
        "$HOME/.claude",
        "${HOME}/.claude",
        "USERPROFILE '.claude",
        'USERPROFILE ".claude',
        ".claude/retrieval_rl_data",
        ".claude\\retrieval_rl_data",
    ):
        assert banned not in joined, (
            f"{script.name} has `{banned}` on a code line — VCO writes nothing "
            f"under ~/.claude that the harness did not ask for"
        )


@pytest.mark.parametrize("script", [_SH, _PS1], ids=["sh", "ps1"])
def test_both_flavours_resolve_through_vct_state_dir(script):
    """Each file names the ONE override, so the rule is greppable in both."""
    text = script.read_text(encoding="utf-8")
    assert "VCT_STATE_DIR" in text
    assert CORPUS_DIRNAME in text


def test_ps1_keeps_its_utf8_bom():
    """Windows PowerShell needs the BOM to read a non-ASCII file as UTF-8.

    These headers contain em-dashes; without the BOM, Windows PowerShell 5.1
    decodes them as mojibake. Every shipped `.ps1` in this tree carries one and
    a rewrite is the easy way to drop it.
    """
    assert _PS1.read_bytes().startswith(b"\xef\xbb\xbf")


def test_sh_has_no_crlf():
    """`.gitattributes` pins `*.sh eol=lf`; a CRLF shebang is `bad interpreter`."""
    assert b"\r\n" not in _SH.read_bytes()
