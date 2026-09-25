# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: ``launcher/tools/vct-cli/install.sh`` removes this CLI's copies
under its OLD names only on positive evidence, and leaves everything else.

The launcher CLI was ``vco`` until v0.2.96, and ``install.sh`` put it at
``~/.local/bin/vco`` — where it hid (or was hidden by) the Python ``vco``
console script. The rename to ``vct-cli`` has to clear that copy, but a
``vco`` at that path may just as well be the Python CLI (``pipx install``
puts it there) or something of the user's. Same for ``~/.local/bin/vct``,
which the pre-v0.1.0 CLI used and which is now the secrets tool: the previous
``install.sh`` deleted ANY regular file there, a copied secrets ``vct``
included.

The rule lives in ``vco_lib/launcher_cli_identity.py`` (the doctor probe
``former_launcher_cli`` reads it too); ``retire-old-names.sh`` only finds a
Python to run it. Each case runs the real script against stand-in executables
that answer ``--version`` / ``-h`` / ``--help`` the way each program does
(checked once against the real pre-rename binary and the real Python ``vco``).
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RETIRE = REPO / "launcher" / "tools" / "vct-cli" / "retire-old-names.sh"
INSTALL = REPO / "launcher" / "tools" / "vct-cli" / "install.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="install.sh is a bash script for Linux/macOS",
)

# `--version` / `-h` / `--help` as each program answers them. clap prints the
# tagline only for `-h`; `--help` shows the long description instead.
OLD_RUST_VCO = (
    'case "$1" in --version) echo "vco 0.2.4";; '
    '-h) echo "vibecoded-orchestrator CLI — power-user / CI escape hatch.";; '
    "--help) echo \"Talks to the running VCT Launcher's local hub server\";; esac"
)
PRE_010_RUST_VCT = (
    'case "$1" in --version) echo "vct 0.1.0";; '
    '-h) echo "VCT Launcher CLI — power-user / CI escape hatch.";; '
    "--help) echo \"Talks to the running VCT Launcher's local hub server\";; esac"
)
PYTHON_VCO = (
    'case "$1" in -h|--help) echo "usage: vco [-h] {verify-pins,doctor,project} ..."; '
    'echo; echo "VibeCoded Orchestrator CLI. Each subcommand maps to an";; '
    '*) echo "vco: error: the following arguments are required: subcommand" >&2; exit 2;; esac'
)
SECRETS_VCT = (
    'case "$1" in --version) echo "vct 3.1.0";; '
    '-h|--help) echo "vct — per-project secret store";; esac'
)
UNKNOWN_VCO = 'echo "vco 1.0 — some other tool"'


def _program(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _retire(bin_dir: Path, home: Path, path_dirs: list[Path]) -> str:
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([*(str(d) for d in path_dirs), "/usr/bin", "/bin"]),
    }
    proc = subprocess.run(
        ["bash", "-c", '. "$1" && vct_cli_retire_old_names "$2"', "_", str(RETIRE), str(bin_dir)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture()
def layout(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    return home, bin_dir


def test_an_old_rust_vco_it_installed_is_removed(layout) -> None:
    home, bin_dir = layout
    _program(bin_dir / "vco", OLD_RUST_VCO)
    out = _retire(bin_dir, home, [bin_dir])
    assert not (bin_dir / "vco").exists()
    assert "Removed" in out and "`vco`" in out


def test_a_python_vco_at_that_path_is_left_alone_silently(layout) -> None:
    home, bin_dir = layout
    vco = _program(bin_dir / "vco", PYTHON_VCO)
    before = vco.read_bytes()
    out = _retire(bin_dir, home, [bin_dir])
    assert vco.read_bytes() == before
    assert out == ""


def test_an_unidentified_vco_is_left_and_the_user_told_what_to_do(layout) -> None:
    home, bin_dir = layout
    vco = _program(bin_dir / "vco", UNKNOWN_VCO)
    out = _retire(bin_dir, home, [bin_dir])
    assert vco.is_file()
    assert "Left" in out and f"rm {shlex.quote(str(vco))}" in out


def test_a_symlinked_vco_is_never_removed(layout, tmp_path: Path) -> None:
    home, bin_dir = layout
    target = _program(tmp_path / "elsewhere" / "vco-real", OLD_RUST_VCO)
    (bin_dir / "vco").symlink_to(target)
    _retire(bin_dir, home, [bin_dir])
    assert (bin_dir / "vco").is_symlink()
    assert target.is_file()


def test_a_pre_010_vct_is_removed_and_the_secrets_symlink_restored(layout) -> None:
    home, bin_dir = layout
    _program(bin_dir / "vct", PRE_010_RUST_VCT)
    secrets = _program(home / ".vct-secrets" / "vct", SECRETS_VCT)
    out = _retire(bin_dir, home, [bin_dir])
    assert (bin_dir / "vct").is_symlink()
    assert os.readlink(bin_dir / "vct") == str(secrets)
    assert "Restoring symlink" in out


def test_a_copied_secrets_vct_is_left_alone(layout) -> None:
    """The previous install.sh deleted any regular file at ~/.local/bin/vct."""
    home, bin_dir = layout
    vct = _program(bin_dir / "vct", SECRETS_VCT)
    before = vct.read_bytes()
    _retire(bin_dir, home, [bin_dir])
    assert vct.read_bytes() == before


def test_an_old_copy_elsewhere_on_path_is_reported_not_removed(layout, tmp_path: Path) -> None:
    home, bin_dir = layout
    other = tmp_path / "opt" / "bin"
    stray = _program(other / "vco", OLD_RUST_VCO)
    out = _retire(bin_dir, home, [bin_dir, other])
    assert stray.is_file()
    assert "NOTE" in out and f"rm {shlex.quote(str(stray))}" in out


def test_install_sh_retires_the_old_vco_and_installs_vct_cli(tmp_path: Path) -> None:
    """End to end through the real install.sh, with ``cargo`` stubbed and the
    release binary pre-built: the old copy goes, ``vct-cli`` arrives."""
    checkout = tmp_path / "checkout"
    crate = checkout / "launcher" / "tools" / "vct-cli"
    crate.mkdir(parents=True)
    for script in (INSTALL, RETIRE):
        shutil.copy2(script, crate / script.name)
    (checkout / "vco_lib").mkdir()
    for module in ("__init__.py", "launcher_cli_identity.py", "remedy_shell.py"):
        shutil.copy2(REPO / "vco_lib" / module, checkout / "vco_lib" / module)
    _program(crate / "target" / "release" / "vct-cli", 'echo "vct-cli 0.2.4"')
    stubs = tmp_path / "stubs"
    _program(stubs / "cargo", "exit 0")
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    _program(bin_dir / "vco", OLD_RUST_VCO)

    proc = subprocess.run(
        ["bash", str(crate / "install.sh")],
        env={"HOME": str(home), "PATH": os.pathsep.join([str(stubs), str(bin_dir), "/usr/bin", "/bin"])},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (bin_dir / "vco").exists()
    assert (bin_dir / "vct-cli").read_text(encoding="utf-8").endswith('echo "vct-cli 0.2.4"\n')
    assert "Try: vct-cli --help" in proc.stdout
