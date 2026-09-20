# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.95 WP-7 — the Python cascade, proved by BEHAVIOUR rather than by
reading the scripts.

The parity module next door (``test_v0295_wp7_bootstrap_cascade_parity.py``)
compares the literal lists across the entry points. That locks them to each
other, but it still only *reads* source. These tests instead **run** each
shim against stub interpreters and assert which one it actually invoked — so
they fail when the ordering or the version gate stops working, not merely
when a list is edited.

Isolation (these scripts download and install things when run for real):

* ``HOME`` is redirected into ``tmp_path``.
* The shim is copied into a scratch "repo" directory that contains no
  ``install.py``, no ``scripts/post-install-launcher.sh`` and no ``.venv`` —
  so the shim's step 3 (launcher post-install) is skipped by its own
  ``[ -x scripts/post-install-launcher.sh ]`` guard and nothing is built.
* ``CI=1`` + ``VCT_NON_INTERACTIVE=1`` force every prompt off, so no
  package-manager or ``sudo`` path can be reached.
* The stub interpreters never execute ``install.py``: they answer the
  version probe and otherwise record the name they were invoked under and
  exit 0. No network, no package manager, no real install root.

**Why every cascade name gets a stub**, including the ones a given test wants
"missing": ``PATH`` must retain ``/usr/bin`` for coreutils, and this machine
has real ``python3.x`` binaries there which would otherwise be found by the
cascade and defeat the isolation. A stub that *fails the version probe* takes
exactly the same branch in every shim as a name that is absent (``continue``),
so the scenarios are equivalent while remaining hermetic.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The canonical cascade, newest first.
CASCADE = ["python3.13", "python3.12", "python3.11", "python3", "python"]


def _make_stub(bin_dir: Path, name: str, *, supported: bool) -> None:
    """Write a fake interpreter that records the name it was invoked under.

    The ``-c <probe>`` call is the version gate. The shims read it two
    different ways and the stub satisfies both:

    * ``install.sh`` / ``install.ps1`` parse the printed ``"%d.%d"``;
    * ``first-install.{sh,command}`` read the exit status.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    version = "3.12" if supported else "3.9"
    rc = 0 if supported else 1
    stub = bin_dir / name
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then\n'
        f"    printf '%s' '{version}'\n"
        f"    exit {rc}\n"
        "fi\n"
        f'printf \'%s\\n\' "{name}" >> "$VCT_STUB_MARKER"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_shim(tmp_path: Path, shim: str, usable: list[str]) -> tuple[list[str], str]:
    """Run ``shim`` in an isolated scratch tree.

    ``usable`` names the interpreters that pass the >= 3.11 gate; every other
    cascade name is stubbed as unusable. Returns (interpreters invoked,
    combined output).
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / shim, repo / shim)

    bin_dir = tmp_path / "bin"
    for name in CASCADE:
        _make_stub(bin_dir, name, supported=name in usable)

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    marker = tmp_path / "invoked.txt"

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "VCT_STUB_MARKER": str(marker),
        # Force every interactive prompt off — nothing may install or block.
        "CI": "1",
        "VCT_NON_INTERACTIVE": "1",
        "VCT_NO_AUTO_LAUNCH": "1",
        "VCT_NO_DESKTOP_ICON": "1",
    }

    res = subprocess.run(
        ["bash", shim, "--no-auto-launch"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    used = []
    if marker.exists():
        used = [ln for ln in marker.read_text(encoding="utf-8").splitlines() if ln]
    return used, (res.stdout + res.stderr)


# ---------------------------------------------------------------------------
# first-install.sh
# ---------------------------------------------------------------------------


def test_first_install_sh_prefers_the_newest_interpreter_present(tmp_path):
    """With 3.11 and 3.12 both usable, the shim must invoke 3.12.

    This is the ordering guarantee, observed rather than read: reversing the
    cascade in first-install.sh flips this to ``python3.11``.
    """
    used, out = _run_shim(tmp_path, "first-install.sh", ["python3.11", "python3.12"])
    assert used, f"first-install.sh never invoked an interpreter. Output:\n{out}"
    assert set(used) == {"python3.12"}, (
        f"first-install.sh resolved {sorted(set(used))!r}; expected only "
        "'python3.12'. The cascade must probe newest-first."
    )


def test_first_install_sh_falls_through_to_bare_python3(tmp_path):
    """Only ``python3``/``python`` usable → ``python3`` wins (it precedes
    ``python`` in the cascade)."""
    used, out = _run_shim(tmp_path, "first-install.sh", ["python3", "python"])
    assert set(used) == {"python3"}, (
        f"first-install.sh resolved {sorted(set(used))!r}; expected 'python3'."
        f"\nOutput:\n{out}"
    )


def test_first_install_sh_skips_a_newer_name_that_fails_the_version_gate(tmp_path):
    """A newer *name* that fails the ``>= 3.11`` probe must be skipped.

    Pins the gate itself: ``python3.13`` is first in the cascade but reports
    an unsupported version, so ``python3.11`` must be chosen instead.
    """
    used, out = _run_shim(tmp_path, "first-install.sh", ["python3.11"])
    assert set(used) == {"python3.11"}, (
        f"first-install.sh resolved {sorted(set(used))!r}; expected "
        "'python3.11' — candidates failing the >=3.11 probe must be skipped, "
        f"not used.\nOutput:\n{out}"
    )


def test_first_install_sh_fails_loudly_when_no_interpreter_qualifies(tmp_path):
    """No usable Python → non-zero exit and a hint, never a silent success."""
    used, out = _run_shim(tmp_path, "first-install.sh", [])
    assert not used, (
        f"first-install.sh invoked {used!r} even though no candidate passed "
        "the version gate"
    )
    assert "Python 3.11+ required" in out, (
        f"expected the missing-Python message; got:\n{out}"
    )


# ---------------------------------------------------------------------------
# first-install.command (macOS shim, but pure bash — runs here too)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    Path("/opt/homebrew").exists(),
    reason="an Apple-Silicon Homebrew prefix would shadow the PATH stubs",
)
def test_first_install_command_prefers_the_newest_interpreter_present(tmp_path):
    """The macOS shim honours the same ordering once its Homebrew prefixes
    miss (they do not exist on this host)."""
    used, out = _run_shim(
        tmp_path, "first-install.command", ["python3.11", "python3.12"]
    )
    assert used, f"first-install.command never invoked an interpreter:\n{out}"
    assert set(used) == {"python3.12"}, (
        f"first-install.command resolved {sorted(set(used))!r}; expected "
        "'python3.12'."
    )


# ---------------------------------------------------------------------------
# install.sh — the CLI/automation entry point
# ---------------------------------------------------------------------------


def test_install_sh_prefers_the_newest_interpreter_present(tmp_path):
    """install.sh ends in ``exec "$PYTHON" install.py`` — assert WHICH python.

    ``CI=1`` puts the script in non-interactive mode, so its Node/Podman
    ladders only warn; nothing is installed and no prompt blocks.
    """
    used, out = _run_shim(tmp_path, "install.sh", ["python3.11", "python3.12"])
    assert used, f"install.sh never invoked an interpreter at all:\n{out}"
    assert set(used) == {"python3.12"}, (
        f"install.sh resolved {sorted(set(used))!r}; expected 'python3.12'. "
        "install.sh must probe the same cascade, newest-first, as the shims."
    )


def test_install_sh_and_first_install_sh_resolve_the_same_interpreter(tmp_path):
    """The two POSIX entry points must never disagree on the same machine.

    This is the user-visible failure the cascade parity exists to prevent:
    one entry point succeeding while the other reports "Python not found" (or
    silently using an older interpreter) on an identical box.
    """
    usable = ["python3.11", "python3.13"]
    via_install, out_a = _run_shim(tmp_path / "a", "install.sh", list(usable))
    via_first, out_b = _run_shim(tmp_path / "b", "first-install.sh", list(usable))

    assert via_install and via_first, (
        f"one entry point invoked nothing: install.sh={via_install!r} "
        f"first-install.sh={via_first!r}\n--- install.sh ---\n{out_a}\n"
        f"--- first-install.sh ---\n{out_b}"
    )
    assert set(via_install) == set(via_first) == {"python3.13"}, (
        "the two POSIX entry points disagreed on the interpreter:\n"
        f"  install.sh       -> {sorted(set(via_install))!r}\n"
        f"  first-install.sh -> {sorted(set(via_first))!r}\n"
        "Both must pick python3.13 (newest usable)."
    )
