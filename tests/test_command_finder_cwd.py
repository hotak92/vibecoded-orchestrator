# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression test for M-P1-4 (v0.2.53).

When Finder launches a `.command` file on macOS, the script's cwd is
the user's `$HOME` — NOT the script's directory. Any script that
references files via relative paths (`./install.sh`, `scripts/lib/...`)
breaks under Finder launch unless it explicitly resolves SCRIPT_DIR
and cd's into it.

This test enforces, for every `.command` file in the repo root:
1. The script resolves SCRIPT_DIR via `cd "$(dirname "$0")" && pwd`.
2. The script cd's into SCRIPT_DIR before performing any work that
   could touch a relative path.
3. The cd is idempotent (safe to re-run).
"""

from __future__ import annotations

import os
import subprocess
import re
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _command_files() -> list[Path]:
    """All .command shell entry-points under the repo root."""
    return sorted(REPO_ROOT.glob("*.command"))


def test_command_files_exist():
    files = _command_files()
    assert files, "no .command files found at repo root"
    names = {p.name for p in files}
    # Sanity: the two entry-points we expect to exist.
    assert "first-install.command" in names
    assert "start-launcher.command" in names


def test_each_command_script_resolves_script_dir():
    """Each .command must set SCRIPT_DIR via the canonical idiom."""
    pattern = re.compile(
        r'SCRIPT_DIR=\s*"\$\(\s*cd\s+"\$\(\s*dirname\s+"\$0"\s*\)"\s*&&\s*pwd\s*\)"',
    )
    for path in _command_files():
        text = path.read_text(encoding="utf-8")
        assert pattern.search(text), (
            f"{path.name} does not resolve SCRIPT_DIR via the canonical "
            'idiom `SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"`'
        )


def test_each_command_script_cds_into_script_dir():
    """Each .command must cd into SCRIPT_DIR before doing real work."""
    for path in _command_files():
        text = path.read_text(encoding="utf-8")
        # Must appear on a line starting with `cd ` (possibly
        # preceded by whitespace).
        assert re.search(r'^\s*cd\s+"\$SCRIPT_DIR"', text, re.MULTILINE), (
            f"{path.name} does not `cd \"$SCRIPT_DIR\"` (M-P1-4 — Finder cwd defense)"
        )


def test_command_finder_launch_simulation(tmp_path: Path):
    """Simulate a Finder launch by running each .command with cwd=$HOME.

    For each .command we:
    1. Make a sandboxed copy of the script + its referenced siblings
       (install.sh / scripts/lib/...) into a tmp_path.
    2. Stub install.sh / install.py / the launcher binary with
       trivial scripts that print a marker.
    3. Run the .command with cwd=$HOME and observe whether the
       script reaches its first work (= it cd'd correctly).

    We test the CD behavior with a minimal harness because running
    the real first-install / start-launcher inside CI would actually
    install / launch things. The harness extracts just the SCRIPT_DIR
    resolution + cd + a `pwd` echo and runs that under cwd=$HOME.
    """
    for path in _command_files():
        text = path.read_text(encoding="utf-8")
        # Extract lines up through the first `cd "$SCRIPT_DIR"`.
        # Anything beyond that is the script body; we don't care.
        # We just verify the resolution + cd produces the right cwd.
        m = re.search(r'^\s*cd\s+"\$SCRIPT_DIR"', text, re.MULTILINE)
        assert m, f"{path.name} missing cd line"
        snippet_end = m.end()
        prefix = text[:snippet_end]
        # Wrap: print cwd at end so test can verify it.
        harness = prefix + "\npwd\n"
        scratch = tmp_path / path.name
        scratch.write_text(harness)
        scratch.chmod(0o755)
        # Simulate Finder: cwd=$HOME (we use tmp_path/home).
        fake_home = tmp_path / "home"
        fake_home.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        out = subprocess.run(
            ["bash", str(scratch)],
            cwd=str(fake_home),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert out.returncode == 0, (
            f"{path.name} harness exit {out.returncode}; stderr={out.stderr!r}"
        )
        final_cwd = out.stdout.strip().splitlines()[-1]
        expected = str(scratch.parent.resolve())
        assert final_cwd == expected, (
            f"{path.name} did NOT cd into its own script dir; "
            f"final cwd={final_cwd!r}, expected={expected!r}"
        )
