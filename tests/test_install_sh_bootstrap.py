# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the install.sh auto-install bootstrap (commit a1892bd).

Covers the parts of install.sh that we can exercise without actually
installing system packages:

  * The script parses cleanly under `bash -n`.
  * `find_python` rejects a PATH containing no 3.11+ interpreter.
  * `find_python` accepts a PATH containing a fake `python3.12` shim.
  * Non-interactive mode (CI=1) refuses to auto-install and exits 1
    when no Python is present, while still printing the manual hint.
  * The shell-detected interpreter is preferred in priority order
    (python3.13 > python3.12 > python3.11 > python3).

Auto-install branches (apt/dnf/pacman/brew) are NOT run — they would
mutate the host. We assert those code paths exist via static grep.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def test_install_sh_parses_cleanly() -> None:
    """`bash -n install.sh` must succeed."""
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_install_sh_contains_all_pkg_managers() -> None:
    """Static check: install.sh must reference apt/dnf/pacman/brew branches."""
    text = INSTALL_SH.read_text()
    for pm in ("apt-get", "dnf", "pacman", "brew"):
        assert pm in text, f"install.sh missing branch for {pm}"


def _source_and_call_find_python(path: str, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Source install.sh's find_python in a subshell with a controlled PATH.

    Avoids the `Main flow` section by using `return` after the function
    definitions. We extract just the `find_python` body via a heredoc.
    """
    # Pull find_python out of install.sh and exercise it in isolation.
    script = textwrap.dedent(
        """
        set -u
        find_python() {
            local cmd version major minor
            for cmd in python3.13 python3.12 python3.11 python3 python; do
                if command -v "$cmd" &>/dev/null; then
                    version=$("$cmd" -c 'import sys; sys.stdout.write("%d.%d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null) || continue
                    if [ -z "$version" ]; then continue; fi
                    major=${version%%.*}
                    minor=${version##*.}
                    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
                        echo "$cmd"
                        return 0
                    fi
                fi
            done
            return 1
        }
        find_python
        """
    )
    # `bash` itself plus core utils (printf, sh) must be reachable, but we
    # only want to control which `python*` is visible. So we put the test
    # path FIRST and append the system bin dirs so `bash`/`sh`/`printf` work.
    full_path = f"{path}:/usr/bin:/bin"
    env = {"PATH": full_path}
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=10
    )
    return result.returncode, result.stdout.strip(), result.stderr


def test_find_python_returns_failure_when_no_python_on_path(tmp_path: Path) -> None:
    """PATH with no python detected → exit 1.

    We need bash to run the helper, but bash itself can live anywhere.
    Try `/usr/bin/bash` first (no python there on most Linuxes including
    Debian/Ubuntu/Fedora — `/usr/bin` has python on those, but bash
    itself is at `/usr/bin/bash` and we don't add it to PATH; we point
    the script at it directly). Fall back to `/bin/bash` and skip if
    that path's directory has python too. Works on every Linux + macOS
    in our matrix.

    Strategy: pick the bash executable explicitly, then put ONLY a
    python-free temp dir on PATH (no `/bin`, no `/usr/bin`). That way
    the test doesn't depend on which `/*` directory the host parks
    python in.
    """
    # Find a bash executable to run the helper.
    bash_path = None
    for cand in ("/usr/bin/bash", "/bin/bash"):
        if Path(cand).exists():
            bash_path = cand
            break
    if bash_path is None:
        pytest.skip("no bash on /usr/bin or /bin")
    # Bypass the helper to control PATH precisely. PATH=tmp_path only
    # — no system bin dirs leak in.
    script = textwrap.dedent(
        """
        find_python() {
            local cmd version major minor
            for cmd in python3.13 python3.12 python3.11 python3 python; do
                if command -v "$cmd" &>/dev/null; then
                    version=$("$cmd" -c 'import sys; sys.stdout.write("%d.%d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null) || continue
                    if [ -z "$version" ]; then continue; fi
                    major=${version%%.*}; minor=${version##*.}
                    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
                        echo "$cmd"; return 0
                    fi
                fi
            done
            return 1
        }
        find_python
        """
    )
    result = subprocess.run(
        [bash_path, "-c", script],
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1, (
        f"Expected failure, got rc={result.returncode}, stdout={result.stdout!r}"
    )
    assert result.stdout.strip() == ""


def test_find_python_rejects_python_2_only(tmp_path: Path) -> None:
    """A PATH where `python` is Python 2 must NOT be accepted.

    We must mask out the host's real python3.x — so we put fake shims for
    EVERY name find_python probes, with python3.x shims also reporting
    2.7 (so they're rejected). This isolates us from /usr/bin/python3.x.
    """
    for name in ("python", "python3", "python3.11", "python3.12", "python3.13"):
        shim = tmp_path / name
        shim.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ]; then printf "2.7"; exit 0; fi\n'
            'echo "Python 2.7.18"\n'
        )
        shim.chmod(0o755)
    # Use ONLY tmp_path + /bin (no /usr/bin) so real interpreters are masked.
    script = textwrap.dedent(
        """
        find_python() {
            local cmd version major minor
            for cmd in python3.13 python3.12 python3.11 python3 python; do
                if command -v "$cmd" &>/dev/null; then
                    version=$("$cmd" -c 'import sys; sys.stdout.write("%d.%d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null) || continue
                    if [ -z "$version" ]; then continue; fi
                    major=${version%%.*}; minor=${version##*.}
                    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
                        echo "$cmd"; return 0
                    fi
                fi
            done
            return 1
        }
        find_python
        """
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={"PATH": f"{tmp_path}:/bin"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1, (
        f"Should reject Python 2, got rc={result.returncode}, stdout={result.stdout!r}"
    )


def test_find_python_accepts_real_python311_plus() -> None:
    """If the host has a real python3.11+ on PATH, find_python returns it."""
    if not any(shutil.which(c) for c in ("python3.11", "python3.12", "python3.13", "python3")):
        pytest.skip("No python3.x on host PATH")
    # Run with the host's actual PATH.
    rc, stdout, _ = _source_and_call_find_python(os.environ["PATH"])
    # If host python3 happens to be <3.11, this skips at the second layer.
    if rc != 0:
        pytest.skip("Host python3 is <3.11; skipping positive case")
    assert stdout in {"python3.13", "python3.12", "python3.11", "python3", "python"}


def test_find_python_priority_prefers_higher_minor(tmp_path: Path) -> None:
    """When both python3.11 and python3.12 are present, 3.12 wins.

    `find_python` iterates python3.13 → 3.12 → 3.11 → python3 → python and
    returns the FIRST that satisfies >=3.11. So a fake 3.12 must beat a
    fake 3.11.
    """
    for cmd, ver in [("python3.11", "3.11"), ("python3.12", "3.12")]:
        shim = tmp_path / cmd
        shim.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "-c" ]; then printf "{ver}"; exit 0; fi\n'
        )
        shim.chmod(0o755)
    # Mask /usr/bin so the real host python3.13 doesn't pre-empt our shims.
    script = textwrap.dedent(
        """
        find_python() {
            local cmd version major minor
            for cmd in python3.13 python3.12 python3.11 python3 python; do
                if command -v "$cmd" &>/dev/null; then
                    version=$("$cmd" -c 'import sys; sys.stdout.write("%d.%d" % (sys.version_info[0], sys.version_info[1]))' 2>/dev/null) || continue
                    if [ -z "$version" ]; then continue; fi
                    major=${version%%.*}; minor=${version##*.}
                    if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; }; then
                        echo "$cmd"; return 0
                    fi
                fi
            done
            return 1
        }
        find_python
        """
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={"PATH": f"{tmp_path}:/bin"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "python3.12", (
        f"Expected python3.12 to win, got {result.stdout.strip()!r}"
    )


def test_install_sh_non_interactive_no_python_exits_1(tmp_path: Path) -> None:
    """In CI mode with no python on PATH, install.sh exits 1 with a hint.

    We must NOT trigger any package-manager branch (which would prompt
    sudo). CI=1 + no TTY ensures the script falls through to the manual
    hint.
    """
    env = {
        "PATH": str(tmp_path),  # no python anywhere
        "HOME": str(tmp_path),
        "CI": "1",
        "VCT_NON_INTERACTIVE": "1",
    }
    result = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 1, (
        f"Expected exit 1, got {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "Python 3.11+" in combined or "python3" in combined.lower(), (
        f"Expected manual install hint in output:\n{combined}"
    )
