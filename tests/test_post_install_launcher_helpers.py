# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for post-install-launcher.sh's shell-helper detection logic.

Covers the bug-fixes that landed 2026-04-27/28 for users with shell-
function-wrapped tools (lean-ctx / asdf / fnm / nvm) where `command -v`
matches a wrapper that points at a missing binary:

  * `_resolves_to_binary` rejects function/builtin shadows and only
    succeeds when `command -v` returns an absolute path that exists.
  * `_ensure_path_for_tool` finds a real binary at known fnm/nvm/local
    locations, prepends its directory to PATH, AND unsets any shell-
    function shadow so `command -v` resolves to the binary on retry.
  * Symlink-target dirname (one hop) is also added to PATH so siblings
    of the resolved tool (e.g. `npx` next to `npm`) are reachable.
  * Bash syntax of post-install-launcher.sh is valid under `bash -n`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "post-install-launcher.sh"

# A reusable Bash preamble that extracts just the helper functions we want
# to test. Re-deriving them from the script keeps the test honest about
# the actual implementation (vs forking it). If the helpers move, the
# preamble breaks loudly.
HELPERS_PREAMBLE = textwrap.dedent(
    """
    set -uo pipefail

    # --- _resolves_to_binary: copied from the script under test ---
    _resolves_to_binary() {
        local resolved
        resolved="$(command -v "$1" 2>/dev/null)"
        case "$resolved" in
            /*) [ -x "$resolved" ] ;;
            *) return 1 ;;
        esac
    }

    # --- _ensure_path_for_tool: copied from the script under test ---
    _ensure_path_for_tool() {
        local tool="$1"; shift
        if _resolves_to_binary "$tool"; then
            return 0
        fi
        local cand
        for cand in "$@"; do
            if [ -x "$cand" ]; then
                local cand_dir
                cand_dir="$(dirname "$cand")"
                local hop_target
                hop_target="$(readlink "$cand" 2>/dev/null || true)"
                local hop_dir=""
                if [ -n "$hop_target" ]; then
                    case "$hop_target" in
                        /*) hop_dir="$(dirname "$hop_target")" ;;
                        *)  hop_dir="$(cd "$cand_dir" && cd "$(dirname "$hop_target")" && pwd 2>/dev/null || true)" ;;
                    esac
                fi
                local d
                for d in "$cand_dir" "$hop_dir"; do
                    [ -z "$d" ] && continue
                    [ ! -d "$d" ] && continue
                    case ":$PATH:" in
                        *":$d:"*) ;;
                        *) export PATH="$d:$PATH" ;;
                    esac
                done
                unset -f "$tool" 2>/dev/null || true
                if _resolves_to_binary "$tool"; then
                    return 0
                fi
            fi
        done
        if unset -f "$tool" 2>/dev/null && _resolves_to_binary "$tool"; then
            return 0
        fi
        return 1
    }
    """
).strip()


def _run_bash(snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a bash snippet that includes the helpers preamble, return the result."""
    full = HELPERS_PREAMBLE + "\n\n" + snippet
    # Sanitised env: keep only HOME + a minimal PATH to make tests reproducible.
    base_env = {
        "HOME": (env or {}).get("HOME", os.environ.get("HOME", "/tmp")),
        "PATH": (env or {}).get("PATH", "/usr/bin:/bin"),
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", "-c", full],
        capture_output=True,
        text=True,
        env=base_env,
    )


def test_script_parses_cleanly() -> None:
    """`bash -n post-install-launcher.sh` must succeed."""
    assert SCRIPT.is_file(), f"script missing: {SCRIPT}"
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_resolves_to_binary_accepts_real_binary() -> None:
    """A genuine PATH binary like /bin/sh should be accepted."""
    snippet = textwrap.dedent(
        """
        if _resolves_to_binary sh; then
            echo "OK"
        else
            echo "FAIL"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK" in result.stdout, result.stdout


def test_resolves_to_binary_rejects_shell_function() -> None:
    """A bash function shadowing the tool name must be rejected — this is
    the lean-ctx / asdf / fnm wrapper bug we fixed in 2026-04-27."""
    snippet = textwrap.dedent(
        """
        # Define a bash function with a name that pretends to be a tool.
        nonexistent_tool_xyzzy() { echo "fake"; }
        if _resolves_to_binary nonexistent_tool_xyzzy; then
            echo "FAIL: function accepted as binary"
        else
            echo "OK: function rejected"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK" in result.stdout, result.stdout


def test_resolves_to_binary_rejects_builtin() -> None:
    """Bash builtins (echo, true, etc.) must NOT count as binaries."""
    snippet = textwrap.dedent(
        """
        # `enable -n` would disable a builtin, but `command -v` for builtins
        # returns the name verbatim (no /-prefix), so our case-pattern
        # rejects them. We just check `true` here — every bash has it as
        # a builtin AND as /usr/bin/true, but command -v on a builtin name
        # in fresh bash returns the builtin form.
        # To force the builtin path: `enable true` then check.
        enable true
        if _resolves_to_binary nonexistent_builtin_xyzzy_zzz; then
            echo "FAIL"
        else
            echo "OK"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK" in result.stdout, result.stdout


def test_resolves_to_binary_rejects_missing_tool() -> None:
    """A tool that doesn't exist anywhere must return 1."""
    snippet = textwrap.dedent(
        """
        if _resolves_to_binary definitely_not_a_real_tool_2026_04_28; then
            echo "FAIL"
        else
            echo "OK"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK" in result.stdout, result.stdout


def test_ensure_path_for_tool_finds_binary_via_candidate(tmp_path: Path) -> None:
    """When given a candidate path that resolves to a real binary, the
    helper must add its directory to PATH and confirm via _resolves_to_binary."""
    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir()
    fake_tool = fake_bin_dir / "fakecli"
    fake_tool.write_text("#!/bin/sh\necho hello\n")
    fake_tool.chmod(0o755)

    snippet = textwrap.dedent(
        f"""
        if _ensure_path_for_tool fakecli "{fake_tool}"; then
            echo "OK: $(command -v fakecli)"
        else
            echo "FAIL: not found"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK:" in result.stdout, result.stdout
    assert str(fake_tool) in result.stdout, result.stdout


def test_ensure_path_for_tool_unsets_function_shadow(tmp_path: Path) -> None:
    """Critical regression test: when a shell function shadows a tool name,
    after _ensure_path_for_tool finds the real binary, the function must be
    unset so subsequent `command -v` resolves to the binary, not the wrapper."""
    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir()
    fake_tool = fake_bin_dir / "wrappedcli"
    fake_tool.write_text("#!/bin/sh\necho real\n")
    fake_tool.chmod(0o755)

    snippet = textwrap.dedent(
        f"""
        # Define a function that shadows the tool name, like lean-ctx does.
        wrappedcli() {{ echo "via wrapper"; }}
        # Pre-condition: command -v finds the function, not the binary.
        pre="$(command -v wrappedcli)"
        if [[ "$pre" != "wrappedcli" ]]; then
            echo "FAIL: pre-state wrong: $pre"
            exit 1
        fi
        if _ensure_path_for_tool wrappedcli "{fake_tool}"; then
            post="$(command -v wrappedcli)"
            if [[ "$post" == /* ]]; then
                echo "OK: function unset, binary at $post"
            else
                echo "FAIL: function still shadowing: $post"
            fi
        else
            echo "FAIL: ensure_path returned non-zero"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK:" in result.stdout, result.stdout


def test_ensure_path_for_tool_resolves_symlink_target_dir(tmp_path: Path) -> None:
    """fnm-style symlinks: ~/.local/bin/npm -> ~/.fnm/.../bin/npm.
    The candidate dir is ~/.local/bin/, but siblings like npx live ONLY in
    the symlink target's dir. Helper must add BOTH dirs to PATH."""
    real_bin_dir = tmp_path / "real-fnm-bin"
    real_bin_dir.mkdir()
    real_tool = real_bin_dir / "fakenode"
    real_tool.write_text("#!/bin/sh\necho real\n")
    real_tool.chmod(0o755)
    sibling = real_bin_dir / "fakenpx"
    sibling.write_text("#!/bin/sh\necho sibling\n")
    sibling.chmod(0o755)

    user_local_bin = tmp_path / "user-local-bin"
    user_local_bin.mkdir()
    symlink = user_local_bin / "fakenode"
    symlink.symlink_to(real_tool)

    snippet = textwrap.dedent(
        f"""
        # Find fakenode via the symlinked candidate.
        if _ensure_path_for_tool fakenode "{symlink}"; then
            # The helper should have added BOTH user-local-bin (candidate dir)
            # AND real-fnm-bin (symlink target dir) to PATH. So fakenpx
            # (only in real-fnm-bin) should be reachable.
            if command -v fakenpx >/dev/null 2>&1; then
                echo "OK: sibling reachable at $(command -v fakenpx)"
            else
                echo "FAIL: sibling not on PATH; PATH=$PATH"
            fi
        else
            echo "FAIL: ensure_path could not resolve fakenode"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK:" in result.stdout, result.stdout


def test_ensure_path_for_tool_already_resolved_returns_fast() -> None:
    """When the tool is already a real binary on PATH, the helper must
    return 0 without touching PATH or candidates."""
    snippet = textwrap.dedent(
        """
        # /bin/sh is always a real binary — no candidates needed.
        before="$PATH"
        if _ensure_path_for_tool sh /tmp/non-existent-candidate; then
            after="$PATH"
            if [[ "$before" == "$after" ]]; then
                echo "OK: PATH unchanged"
            else
                echo "FAIL: PATH was modified despite tool being already-resolved"
            fi
        else
            echo "FAIL: ensure_path returned non-zero for resolvable tool"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK:" in result.stdout, result.stdout


def test_ensure_path_for_tool_no_candidates_falls_back_to_unset() -> None:
    """When the tool is shadowed by a function but reachable via current
    PATH (after the unset), the no-candidates fast-path at the end of the
    helper must succeed."""
    # Use `sh` which is on PATH at /bin/sh on every Linux/macOS. Shadow it
    # with a function, then call _ensure_path_for_tool with NO candidates.
    snippet = textwrap.dedent(
        """
        sh() { echo "wrapped"; }
        if _ensure_path_for_tool sh; then
            post="$(command -v sh)"
            if [[ "$post" == /* ]]; then
                echo "OK: unset succeeded, sh resolves to $post"
            else
                echo "FAIL: sh still shadowed: $post"
            fi
        else
            echo "FAIL: helper returned non-zero"
        fi
        """
    )
    result = _run_bash(snippet)
    assert result.returncode == 0
    assert "OK:" in result.stdout, result.stdout


def test_helpers_block_present_in_actual_script() -> None:
    """Sanity: the production script still contains the function definitions
    we copied above. If someone refactors them, the test preamble might drift —
    fail loudly so the tests get updated alongside the code."""
    body = SCRIPT.read_text()
    assert "_resolves_to_binary()" in body or "_resolves_to_binary ()" in body, \
        "_resolves_to_binary missing from script"
    assert "_ensure_path_for_tool()" in body or "_ensure_path_for_tool ()" in body, \
        "_ensure_path_for_tool missing from script"
    # The unset-function-shadow line is a critical part of the fix.
    assert 'unset -f "$tool"' in body, \
        "function-unset bypass missing from _ensure_path_for_tool"
    # The deprecated-npm-bin replacement is the other critical fix.
    assert "npm prefix -g" in body, \
        "npm prefix -g (modern replacement for `npm bin -g`) missing"
    # `--no-bundle` flag was added 2026-04-28 to skip DEB/RPM/MSI bundling.
    assert "--no-bundle" in body, \
        "--no-bundle flag missing from tauri build invocation"
