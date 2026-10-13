# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Runs the self-contained bash test suites under pytest (v0.2.102).

CI runs ``pytest tests/``; a ``*.sh`` test file is invisible to it unless a
pytest file shells out to it. Before v0.2.102 seven bash suites had no such
runner, so they ran only when someone remembered to run them by hand — and
one of them, ``tests/test_code_graph_incremental_hook.sh``, had been failing
9 of 11 asserts for many releases without anyone noticing.

This module is the ONE runner for every bash suite that has no dedicated
pytest wrapper of its own, and :func:`test_every_shell_suite_has_a_runner`
keeps it that way: a new ``test_*.sh`` must either be listed in
``SUITES`` here or be named, with its runner file, in ``EXTERNAL_RUNNERS``.

Skipped on hosts without bash (native Windows); the PowerShell flavor of the
code-graph hook suite is additionally skipped without ``pwsh``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_BASH = shutil.which("bash")
_PWSH = shutil.which("pwsh")

# (suite path relative to the repo root, extra argv, needs pwsh)
SUITES: list[tuple[str, tuple[str, ...], bool]] = [
    # The unregistered standalone code-graph hook, both siblings, same scenarios.
    ("tests/test_code_graph_incremental_hook.sh", ("sh",), False),
    ("tests/test_code_graph_incremental_hook.sh", ("ps1",), True),
    ("tests/test_apple_silicon_homebrew_path_probe.sh", (), False),
    ("tests/test_install_sh_no_args.sh", (), False),
    ("tests/test_kde_plasma6_desktop_launch.sh", (), False),
    ("tests/test_launcher_metadata_reader.sh", (), False),
    ("tests/test_libwebkit2gtk_fallback.sh", (), False),
    ("templates/scripts/test_vct_project_config_rate_limit.sh", (), False),
]

# Suites that already have their own dedicated pytest runner.
EXTERNAL_RUNNERS: dict[str, str] = {
    "tests/test_v02102_shortcut_discovery.sh": "tests/test_v02102_shortcut_discovery.py",
    "tests/test_vct_project_config_extras.sh": "tests/test_vct_project_config_extras_runner.py",
    "tests/test_vct_secrets_resolve.sh": "tests/test_vct_secrets_resolve_sh_runner.py",
    "tools/vct-secrets/tests/test_vct.sh": "tests/test_vct_secrets_cli_suite_runner.py",
    "tools/vct-secrets/tests/test_vct_name_shape_and_shared_scope.sh":
        "tests/test_vct_name_shape_and_shared_scope.py",
}

# Where bash test suites live in this repo.
_SUITE_GLOBS = ("tests/test_*.sh", "templates/scripts/test_*.sh", "tools/*/tests/test_*.sh")


def _suite_id(entry: tuple[str, tuple[str, ...], bool]) -> str:
    rel, argv, _ = entry
    return Path(rel).stem + (f"[{' '.join(argv)}]" if argv else "")


@pytest.mark.skipif(_BASH is None, reason="no bash on PATH (native Windows host)")
@pytest.mark.parametrize("entry", SUITES, ids=[_suite_id(e) for e in SUITES])
def test_shell_suite_passes(entry: tuple[str, tuple[str, ...], bool]) -> None:
    rel, argv, needs_pwsh = entry
    if needs_pwsh and _PWSH is None:
        pytest.skip("pwsh not on PATH — PowerShell flavor skipped")
    suite = REPO_ROOT / rel
    assert suite.is_file(), f"missing bash suite: {suite}"
    assert _BASH is not None
    proc = subprocess.run(
        [_BASH, str(suite), *argv],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, (
        f"bash suite FAILED: {rel} {' '.join(argv)}\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


def test_every_shell_suite_has_a_runner() -> None:
    """A bash suite nobody runs is how the code-graph hook test rotted."""
    found = sorted(
        str(p.relative_to(REPO_ROOT)).replace("\\", "/")
        for pattern in _SUITE_GLOBS
        for p in REPO_ROOT.glob(pattern)
    )
    assert found, "suite discovery found nothing — the globs no longer match the tree"
    listed_here = {rel for rel, _, _ in SUITES}
    orphans = [s for s in found if s not in listed_here and s not in EXTERNAL_RUNNERS]
    assert not orphans, (
        "bash test suites with no pytest runner (add them to SUITES in "
        f"tests/test_shell_suites_runner.py, or give them a runner): {orphans}"
    )
    # An EXTERNAL_RUNNERS entry must name a runner that exists and refers to
    # the suite by file name — a stale mapping would hide an orphan.
    for suite, runner in EXTERNAL_RUNNERS.items():
        assert (REPO_ROOT / suite).is_file(), f"EXTERNAL_RUNNERS lists a missing suite: {suite}"
        runner_path = REPO_ROOT / runner
        assert runner_path.is_file(), f"runner for {suite} is missing: {runner}"
        assert Path(suite).name in runner_path.read_text(encoding="utf-8"), (
            f"{runner} does not reference {Path(suite).name}"
        )
    # And no suite is claimed twice.
    assert not listed_here & set(EXTERNAL_RUNNERS), "a suite is in both SUITES and EXTERNAL_RUNNERS"
