# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-source dist-subdir parity (spec'd in docs/INSTALL_ARCHITECTURE_v2.md
§10.1, Track A; first implemented v0.2.55).

The OS+arch -> dist subdir mapping (`linux-x64` / `macos-arm64` /
`macos-x64` / `windows-x64`) is encoded in FOUR places that can silently
drift apart:

  1. scripts/post-install-launcher.sh::_canonical_dist_subdir   (bash, POSIX-only)
  2. install.py::_launcher_binary_relative_path                  (python)
  3. launcher/src-tauri/src/commands/restart.rs::launcher_binary_relative_path (rust)
  4. launcher/src-tauri/src/commands/installer.rs::launcher_dist_subdir        (rust)

A pure-shell helper cannot import the Rust/Python resolvers, so extraction
into one shared impl is not feasible across three languages. Instead this
test PINS equality: if any source renames a slot or adds an arch without
the others, the test fails loudly so all four move together.

Why this matters (v0.2.55 launcher self-update bug): the launcher GUI's
restart re-execs from `launcher/dist/<subdir>/vct-launcher`. If the build
path stages into a subdir the restart path doesn't read (because the two
disagreed on the subdir name), the user keeps running the old binary —
exactly the class of failure that motivated the v0.2.55 dist-staging fix.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "post-install-launcher.sh"
INSTALL_PY = REPO_ROOT / "install.py"
RESTART_RS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "restart.rs"
)
INSTALLER_RS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "installer.rs"
)

# The canonical POSIX subdirs the bash helper is responsible for.
_EXPECTED_POSIX = {
    ("linux", "x86_64"): "linux-x64",
    ("macos", "arm64"): "macos-arm64",
    ("macos", "x86_64"): "macos-x64",
}

# Every canonical subdir literal that MUST appear in each Rust resolver.
_ALL_CANONICAL = {"linux-x64", "macos-arm64", "macos-x64", "windows-x64"}


def _extract_function(name: str) -> str:
    """Return the source text of a shell function `name` from SCRIPT.

    Fails loudly if the function can't be found (rename/removal must be
    reflected here).
    """
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{name}() {{") or line.startswith(f"{name} () {{"):
            start = i
            break
    assert start is not None, f"function {name}() not found in {SCRIPT}"
    end = None
    for j in range(start + 1, len(lines)):
        if lines[j] == "}":
            end = j
            break
    assert end is not None, f"could not find closing brace for {name}()"
    return "\n".join(lines[start : end + 1])


_STUBS = "set -uo pipefail\n"


def _bash_canonical_subdir(os_name: str, machine: str) -> str | None:
    fn = _extract_function("_canonical_dist_subdir")
    body = (
        f"{_STUBS}\nOS={os_name}\n"
        f"uname() {{ echo {machine}; }}\n"
        f"{fn}\n"
        "if _canonical_dist_subdir; then :; else echo __NONE__; fi"
    )
    res = subprocess.run(
        ["bash", "-c", body],
        capture_output=True,
        text=True,
        timeout=30,
        env=dict(os.environ),
    )
    out = res.stdout.strip()
    return None if out in ("__NONE__", "") else out


def test_bash_canonical_subdir_matches_expected_posix():
    for (os_name, machine), expected in _EXPECTED_POSIX.items():
        got = _bash_canonical_subdir(os_name, machine)
        assert got == expected, (
            f"bash _canonical_dist_subdir({os_name},{machine}) = {got!r}, "
            f"expected {expected!r}"
        )


def test_bash_canonical_subdir_rejects_unknown_os():
    """The bash helper is POSIX-only; Windows is handled by the .bat path,
    so an unknown/Windows OS must return non-zero (no bogus subdir)."""
    assert _bash_canonical_subdir("windows", "amd64") is None
    assert _bash_canonical_subdir("plan9", "x86_64") is None


def test_python_launcher_relative_path_matches_canonical():
    """install.py::_launcher_binary_relative_path agrees with the canonical
    mapping for every POSIX (os, arch) AND Windows."""
    spec = importlib.util.spec_from_file_location(
        "_vco_install_for_parity", INSTALL_PY
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # install.py is import-safe (real work guarded by `if __name__ == '__main__'`).
    spec.loader.exec_module(mod)

    import platform as _platform

    cases = [
        ("Linux", "x86_64", "linux-x64"),
        ("Darwin", "arm64", "macos-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Windows", "amd64", "windows-x64"),
    ]
    orig_system, orig_machine = _platform.system, _platform.machine
    try:
        for sys_name, mach, expected_subdir in cases:
            _platform.system = lambda s=sys_name: s
            _platform.machine = lambda m=mach: m
            subdir, _fname = mod._launcher_binary_relative_path()
            assert subdir == expected_subdir, (
                f"python _launcher_binary_relative_path({sys_name},{mach}) = "
                f"{subdir!r}, expected {expected_subdir!r}"
            )
    finally:
        _platform.system, _platform.machine = orig_system, orig_machine


def test_rust_resolvers_contain_all_canonical_subdirs():
    """Both Rust resolvers must reference every canonical subdir literal, so a
    rename in one Rust file without the others is caught."""
    for rf in (RESTART_RS, INSTALLER_RS):
        text = rf.read_text(encoding="utf-8")
        missing = {s for s in _ALL_CANONICAL if f'"{s}"' not in text}
        assert not missing, (
            f"{rf.name} is missing dist-subdir literal(s): {missing}"
        )


def test_resolvers_do_not_return_experimental_macos_slot():
    """Regression guard (Audit Bug #1, v0.2.14): the pre-fix 'experimental_macOS'
    slot must not be RETURNED by the canonical resolvers — that mismatch
    silently broke macOS bundled-binary lookup (the resolver pointed at an
    empty placeholder dir).

    Scope is the RESOLVERS only (python `_launcher_binary_relative_path` +
    the rust `launcher_dist_subdir` / `launcher_binary_relative_path`). The
    shell candidate-list in post-install-launcher.sh deliberately KEEPS
    `experimental_macOS` as a legacy-checkout *fallback* search path (it
    probes it but never canonicalizes to it), so it's intentionally excluded
    here — the bug was the resolver returning it, not an extra probe.
    """
    # Python resolver: the RETURN statements must not name the stale slot.
    # (The function's docstring legitimately mentions experimental_macOS in
    # its v0.2.14 history note, so we check `return` lines only, not prose.)
    py_text = INSTALL_PY.read_text(encoding="utf-8")
    start = py_text.find("def _launcher_binary_relative_path")
    assert start != -1, "resolver not found in install.py"
    nxt = py_text.find("\ndef ", start + 1)
    py_fn = py_text[start : nxt if nxt != -1 else len(py_text)]
    offending_returns = [
        ln.strip()
        for ln in py_fn.splitlines()
        if ln.lstrip().startswith("return") and "experimental_macOS" in ln
    ]
    assert not offending_returns, (
        "install.py::_launcher_binary_relative_path returns the stale "
        f"experimental_macOS slot (Audit Bug #1 regression): {offending_returns}"
    )

    # Rust resolvers: scan the two resolver source files. (These files don't
    # carry an intentional experimental_macOS fallback list — any occurrence
    # there is the bug.)
    for rf in (RESTART_RS, INSTALLER_RS):
        text = rf.read_text(encoding="utf-8")
        assert "experimental_macOS" not in text, (
            f"{rf.name} reintroduced the stale experimental_macOS slot"
        )


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
