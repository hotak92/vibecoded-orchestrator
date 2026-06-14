# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for post-install-launcher.sh's v0.2.55 dist-staging fix.

Background (v0.2.55 launcher self-update bug):

The launcher GUI "Fetch + Install" button runs update_orchestrator (Rust),
which shells out to `install.py --update`, which runs the desktop-icon step
`scripts/post-install-launcher.sh`. When the bundled binary at
launcher/dist/<arch>/vct-launcher was rejected as stale (its metadata
source_hash didn't match the freshly-pulled launcher subtree — the window
between the source-merge commit and the binary-refresh commit), the script
fell through to a local `tauri build`. The built binary landed in
launcher/src-tauri/target/release/vct-launcher-temp and was NEVER copied into
launcher/dist/<arch>/. But the Rust restart (restart.rs::resolve_target_binary)
ALWAYS relaunches from launcher/dist/<arch>/vct-launcher — so the user kept
running the OLD dist binary and the update appeared to do nothing.

Two fixes are covered here:

  1. `_stage_built_binary_into_dist` copies a freshly-acquired binary
     (from target/release/ or ~/.local/share/) into the canonical
     launcher/dist/<subdir>/vct-launcher slot the Rust restart reads.
  2. `_canonical_dist_subdir` resolves the per-OS+arch dist subdir,
     mirroring install.py::_launcher_binary_relative_path and
     restart.rs::launcher_binary_relative_path.

The tests source the REAL function bodies out of the script (via sed) so
they stay honest against the shipped implementation — if a function is
renamed or its contract changes, the extraction fails loudly.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "post-install-launcher.sh"


def _extract_function(name: str) -> str:
    """Return the source text of a shell function `name` from SCRIPT.

    Extracts from the `name() {` line up to the matching closing brace at
    column 0 (the script's functions all close with a `}` in column 0).
    Fails the test loudly if the function can't be found — that means the
    fix was renamed/removed and the test must be updated alongside.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{name}() {{") or line.startswith(f"{name} () {{"):
            start = i
            break
    assert start is not None, f"function {name}() not found in {SCRIPT}"
    # Walk to the closing brace at column 0.
    end = None
    for j in range(start + 1, len(lines)):
        if lines[j] == "}":
            end = j
            break
    assert end is not None, f"could not find closing brace for {name}()"
    return "\n".join(lines[start : end + 1])


# Stubs the staging functions depend on: _log_event + _json_escape. We
# provide no-op versions so the extracted function bodies run standalone.
_STUBS = textwrap.dedent(
    """
    set -uo pipefail
    _json_escape() { printf '%s' "$1"; }
    _log_event() { :; }  # no-op in tests
    """
)


def _run_bash(body: str, *, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", body],
        capture_output=True,
        text=True,
        timeout=30,
        env=full_env,
    )


# --------------------------------------------------------------------------
# _canonical_dist_subdir
# --------------------------------------------------------------------------

def test_canonical_dist_subdir_linux():
    fn = _extract_function("_canonical_dist_subdir")
    body = f'{_STUBS}\nOS=linux\n{fn}\n_canonical_dist_subdir'
    res = _run_bash(body)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "linux-x64"


def test_canonical_dist_subdir_macos_arm():
    fn = _extract_function("_canonical_dist_subdir")
    # Shadow uname so the test is arch-independent of the host.
    body = (
        f'{_STUBS}\nOS=macos\n'
        'uname() { echo arm64; }\n'
        f'{fn}\n_canonical_dist_subdir'
    )
    res = _run_bash(body)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "macos-arm64"


def test_canonical_dist_subdir_macos_intel():
    fn = _extract_function("_canonical_dist_subdir")
    body = (
        f'{_STUBS}\nOS=macos\n'
        'uname() { echo x86_64; }\n'
        f'{fn}\n_canonical_dist_subdir'
    )
    res = _run_bash(body)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "macos-x64"


def test_canonical_dist_subdir_unknown_os_fails():
    fn = _extract_function("_canonical_dist_subdir")
    body = f'{_STUBS}\nOS=unknown\n{fn}\nif _canonical_dist_subdir; then echo FRESH; else echo NOSUBDIR; fi'
    res = _run_bash(body)
    assert "NOSUBDIR" in res.stdout, res.stdout


# --------------------------------------------------------------------------
# _stage_built_binary_into_dist
# --------------------------------------------------------------------------

def _staging_preamble() -> str:
    subdir_fn = _extract_function("_canonical_dist_subdir")
    stage_fn = _extract_function("_stage_built_binary_into_dist")
    return f"{_STUBS}\n{subdir_fn}\n{stage_fn}\n"


def test_stage_build_output_into_dist_linux(tmp_path: Path):
    """The core fix: a target/release build is copied into launcher/dist/."""
    repo = tmp_path / "repo"
    built = repo / "launcher" / "src-tauri" / "target" / "release" / "vct-launcher-temp"
    built.parent.mkdir(parents=True)
    built.write_text("#!/bin/sh\necho new-binary\n")
    built.chmod(0o755)

    pre = _staging_preamble()
    body = (
        f'{pre}\n'
        f'OS=linux\n'
        f'REPO_ROOT="{repo}"\n'
        f'_stage_built_binary_into_dist "{built}"\n'
    )
    res = _run_bash(body)
    assert res.returncode == 0, res.stderr

    staged = repo / "launcher" / "dist" / "linux-x64" / "vct-launcher"
    assert staged.is_file(), f"binary not staged into dist; stdout={res.stdout} stderr={res.stderr}"
    assert os.access(staged, os.X_OK), "staged binary must be executable"
    assert staged.read_text() == built.read_text()
    # Echoes the staged path so the caller can re-point LAUNCHER_BIN.
    assert str(staged) in res.stdout


def test_stage_overwrites_stale_dist_binary(tmp_path: Path):
    """Staging replaces an existing (stale) dist binary in place."""
    repo = tmp_path / "repo"
    built = repo / "launcher" / "src-tauri" / "target" / "release" / "vct-launcher-temp"
    built.parent.mkdir(parents=True)
    built.write_text("NEW\n")
    built.chmod(0o755)

    stale = repo / "launcher" / "dist" / "linux-x64" / "vct-launcher"
    stale.parent.mkdir(parents=True)
    stale.write_text("OLD\n")
    stale.chmod(0o755)

    pre = _staging_preamble()
    body = (
        f'{pre}\nOS=linux\nREPO_ROOT="{repo}"\n'
        f'_stage_built_binary_into_dist "{built}"\n'
    )
    res = _run_bash(body)
    assert res.returncode == 0, res.stderr
    assert stale.read_text() == "NEW\n", "stale dist binary should be overwritten"


def test_stage_noop_when_already_dist(tmp_path: Path):
    """If find_binary returned the dist binary directly, staging is a no-op
    (returns 0, prints nothing — nothing to copy onto itself)."""
    repo = tmp_path / "repo"
    dist = repo / "launcher" / "dist" / "linux-x64" / "vct-launcher"
    dist.parent.mkdir(parents=True)
    dist.write_text("BUNDLED\n")
    dist.chmod(0o755)

    pre = _staging_preamble()
    body = (
        f'{pre}\nOS=linux\nREPO_ROOT="{repo}"\n'
        f'_stage_built_binary_into_dist "{dist}"\n'
    )
    res = _run_bash(body)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "", "no-op staging must not echo a path"
    assert dist.read_text() == "BUNDLED\n"


def test_stage_skips_macos_app_bundle(tmp_path: Path):
    """A binary inside a .app/Contents/MacOS bundle must NOT be flattened
    into a single dist file (that would strip Info.plist + resources)."""
    repo = tmp_path / "repo"
    inner = (
        repo
        / "Applications"
        / "VCT Launcher.app"
        / "Contents"
        / "MacOS"
        / "vct-launcher"
    )
    inner.parent.mkdir(parents=True)
    inner.write_text("MACHO\n")
    inner.chmod(0o755)

    pre = _staging_preamble()
    body = (
        f'{pre}\nOS=macos\nREPO_ROOT="{repo}"\n'
        'uname() { echo arm64; }\n'
        f'_stage_built_binary_into_dist "{inner}"\n'
    )
    res = _run_bash(body)
    assert res.returncode == 0, res.stderr
    flat = repo / "launcher" / "dist" / "macos-arm64" / "vct-launcher"
    assert not flat.exists(), ".app bundle must not be flattened into dist/"
    assert res.stdout.strip() == ""


def test_stage_missing_source_fails_soft(tmp_path: Path):
    """A nonexistent source binary fails non-zero but does not crash."""
    repo = tmp_path / "repo"
    pre = _staging_preamble()
    body = (
        f'{pre}\nOS=linux\nREPO_ROOT="{repo}"\n'
        f'if _stage_built_binary_into_dist "{repo}/nope/vct-launcher-temp"; then '
        f'echo STAGED; else echo NOSTAGE; fi\n'
    )
    res = _run_bash(body)
    assert "NOSTAGE" in res.stdout, res.stdout


# --------------------------------------------------------------------------
# Defer-to-launcher-update guard (freshness-ordering fix)
# --------------------------------------------------------------------------

def test_defer_guard_string_present():
    """When VCT_AUTO_RESTART_LAUNCHER=1 (running inside the Rust GUI update),
    the script must defer binary acquisition rather than local-build. We
    assert the guard exists + exits early before the build prompt."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'VCT_AUTO_RESTART_LAUNCHER:-0' in text, (
        "defer guard reading VCT_AUTO_RESTART_LAUNCHER missing"
    )
    # The guard must appear BEFORE the interactive build/download prompt
    # ('Launcher binary not found. Choose how to get it') so it short-
    # circuits the wasteful build.
    guard_idx = text.index("VCT_AUTO_RESTART_LAUNCHER:-0")
    prompt_idx = text.index("Choose how to get it")
    assert guard_idx < prompt_idx, (
        "defer guard must short-circuit before the build/download prompt"
    )


def test_script_still_parses():
    res = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr


# NOTE: cross-source dist-subdir PARITY (bash vs python vs rust) lives in
# the canonical `tests/test_launcher_dist_subdir_parity.py` (spec'd in
# docs/INSTALL_ARCHITECTURE_v2.md §10.1, Track A; first implemented in
# v0.2.55). This file owns the staging-behaviour tests only.


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
