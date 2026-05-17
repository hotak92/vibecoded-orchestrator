# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the PowerShell sibling
``scripts/launch-claude-mcp-stack.ps1`` (v0.2.14 Bug #2).

The .ps1 wrapper ports the bash wrapper's logic to PowerShell 5.1+ so
the Windows Scheduled Task and the launcher's lifecycle commands can
boot the Claude MCP container stack without depending on Git Bash /
WSL bash on PATH.

These tests are skipped automatically when no PowerShell runtime
(``pwsh``, the PowerShell Core 7+ binary, or ``powershell.exe`` on
Windows) is on PATH — most Linux CI workers won't have it. When pwsh
IS available, we exercise the same decision matrix as the bash sibling
tests via ``Get-ComposeInvocation``.

Strategy mirrors the bash sibling: dot-source the script in a child
PowerShell process, call the function, assert on its return shape.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.ps1"

# Prefer pwsh (cross-platform PowerShell Core); fall back to
# powershell.exe (Windows in-box PowerShell 5.1) when only that is
# available — useful for native-Windows CI workers.
_PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    _PWSH is None,
    reason=(
        "no PowerShell runtime on PATH (pwsh / powershell.exe). "
        "PS1 wrapper tests skipped — install PowerShell Core 7+ to "
        "exercise this matrix on non-Windows hosts."
    ),
)


def _ps_quote(value: str) -> str:
    """Single-quote a string for embedding in a PowerShell command line.
    PS single-quoted strings are verbatim — only embedded single quotes
    need doubling. Far safer than double-quote interpolation."""
    return "'" + value.replace("'", "''") + "'"


def _call_get_compose_invocation(
    runtime: str,
    gpu_mode: str,
    working_dir: str,
    extra_env: dict | None = None,
) -> tuple[int, str, str]:
    """Dot-source the PS1 script in a child PowerShell process and call
    Get-ComposeInvocation. Emit the formatted invocation on stdout and
    the OverlayMissingWarned flag on stderr for parity with the bash
    sibling's OVERLAY_MISSING_WARNED line."""
    # PowerShell command: dot-source the script (which exposes the
    # functions without running Invoke-Main because $MyInvocation
    # .InvocationName is "."), then call Get-ComposeInvocation and
    # print its formatted form via Format-Invocation.
    ps_cmd = (
        f". {_ps_quote(str(SCRIPT))}; "
        f"$inv = Get-ComposeInvocation "
        f"-Runtime {_ps_quote(runtime)} "
        f"-GpuMode {_ps_quote(gpu_mode)} "
        f"-WorkingDir {_ps_quote(working_dir)}; "
        # If the call failed (Ok=$false), exit with the ErrorCode.
        "if (-not $inv.Ok) { "
        "  [Console]::Error.WriteLine('OVERLAY_MISSING_WARNED=0'); "
        "  exit $inv.ErrorCode "
        "} "
        "Write-Output (Format-Invocation -Invocation $inv); "
        "[Console]::Error.WriteLine('OVERLAY_MISSING_WARNED=' + "
        "(if ($inv.OverlayMissingWarned) {1} else {0})); "
        "exit 0"
    )
    # PS5.1 doesn't accept the `if (..)` expression form inside string
    # concatenation that PS Core does; rewrite to a compatible form.
    ps_cmd = ps_cmd.replace(
        "(if ($inv.OverlayMissingWarned) {1} else {0})",
        "$(if ($inv.OverlayMissingWarned) {1} else {0})",
    )
    env = None
    if extra_env:
        import os
        env = os.environ.copy()
        env.update(extra_env)
    proc = subprocess.run(
        [_PWSH, "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _overlay_warned(stderr: str) -> bool:
    """Parse the OVERLAY_MISSING_WARNED=<0|1> line emitted on stderr."""
    for line in stderr.splitlines():
        if line.startswith("OVERLAY_MISSING_WARNED="):
            return line.endswith("=1")
    return False


def _materialize_overlays(working_dir: Path) -> None:
    """Create stub overlay files (mirrors the bash test fixture)."""
    infra = working_dir / "infrastructure"
    infra.mkdir(parents=True, exist_ok=True)
    (infra / "docker-compose.gpu.yml").write_text("# stub overlay\n")
    (infra / "podman-compose.gpu.yml").write_text("# stub overlay\n")


def _materialize_override(working_dir: Path, name: str = "compose.override.yaml") -> Path:
    """Create a non-empty override file inside working_dir."""
    f = working_dir / name
    f.write_text("services: {}\n")
    return f


# ---------------------------------------------------------------------------
# Syntax / parse smoke test — the script must at minimum be parseable
# by the active PowerShell engine. This catches typos / stray syntax
# errors that would otherwise only surface at boot time.
# ---------------------------------------------------------------------------


def test_script_parses_cleanly():
    """PowerShell's parser should accept the script without errors.
    We use $ExecutionContext.InvokeCommand.GetCommand on a dot-source
    of the file, which forces a full parse without running Invoke-Main.
    """
    # Simplest robust syntax check: -NoExit with an immediate exit
    # after dot-source. If parse fails, PS exits non-zero with a
    # syntax error on stderr.
    proc = subprocess.run(
        [
            _PWSH, "-NoProfile", "-NonInteractive", "-Command",
            f". {_ps_quote(str(SCRIPT))}; exit 0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"PS script failed to dot-source cleanly.\n"
        f"stderr:\n{proc.stderr}\n"
        f"stdout:\n{proc.stdout}"
    )


# ---------------------------------------------------------------------------
# docker x {gpu, cpu}
# ---------------------------------------------------------------------------


def test_docker_gpu_picks_docker_compose_with_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_get_compose_invocation("docker", "gpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == (
        "docker compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml"
    ) or out == (
        "docker compose -f compose.yaml -f infrastructure/docker-compose.gpu.yml"
    ), out
    # Note: docker maps to docker-compose.gpu.yml in the PS port (and
    # in the bash port), so the second form is the expected one.
    assert "docker-compose.gpu.yml" in out
    assert not _overlay_warned(err)


def test_docker_cpu_picks_docker_compose_no_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_get_compose_invocation("docker", "cpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == "docker compose -f compose.yaml"
    assert not _overlay_warned(err)


# ---------------------------------------------------------------------------
# podman-compose x {gpu, cpu}
# ---------------------------------------------------------------------------


def test_podman_compose_gpu_picks_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_get_compose_invocation("podman-compose", "gpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == (
        "podman-compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml"
    )
    assert not _overlay_warned(err)


def test_podman_compose_cpu_no_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_get_compose_invocation("podman-compose", "cpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == "podman-compose -f compose.yaml"
    assert not _overlay_warned(err)


# ---------------------------------------------------------------------------
# podman compose (subcommand form) x {gpu, cpu}
# ---------------------------------------------------------------------------


def test_podman_subcommand_gpu_picks_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_get_compose_invocation("podman compose", "gpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == (
        "podman compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml"
    )
    assert not _overlay_warned(err)


def test_podman_subcommand_cpu_no_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_get_compose_invocation("podman compose", "cpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == "podman compose -f compose.yaml"
    assert not _overlay_warned(err)


# ---------------------------------------------------------------------------
# Inline-GPU fall-through when the overlay file is missing.
# Mirrors the bash sibling's L1 tests.
# ---------------------------------------------------------------------------


def test_podman_compose_gpu_missing_overlay_falls_through(tmp_path: Path):
    rc, out, err = _call_get_compose_invocation("podman-compose", "gpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == "podman-compose -f compose.yaml"
    assert _overlay_warned(err), (
        f"OverlayMissingWarned must be set when gpu_mode=gpu and the "
        f"overlay file does not exist. stderr={err}"
    )


def test_docker_gpu_missing_overlay_falls_through(tmp_path: Path):
    rc, out, err = _call_get_compose_invocation("docker", "gpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == "docker compose -f compose.yaml"
    assert _overlay_warned(err)


def test_cpu_mode_never_warns_even_when_overlay_missing(tmp_path: Path):
    rc, out, err = _call_get_compose_invocation("podman-compose", "cpu", str(tmp_path))
    assert rc == 0, f"stderr={err}"
    assert out == "podman-compose -f compose.yaml"
    assert not _overlay_warned(err)


# ---------------------------------------------------------------------------
# Soft-fail cases (matches bash sibling exit codes 1 + 2).
# ---------------------------------------------------------------------------


def test_empty_runtime_returns_nonzero(tmp_path: Path):
    rc, out, _ = _call_get_compose_invocation("", "gpu", str(tmp_path))
    assert rc == 1
    assert out == ""


def test_unknown_runtime_returns_nonzero(tmp_path: Path):
    rc, out, _ = _call_get_compose_invocation("docker-fake", "gpu", str(tmp_path))
    assert rc == 2
    assert out == ""


# ---------------------------------------------------------------------------
# VCT_STACK_COMPOSE_OVERRIDE explicit -f emission (mirrors PR-22 bash tests)
# ---------------------------------------------------------------------------


def test_override_present_emits_explicit_f_flag(tmp_path: Path):
    _materialize_override(tmp_path)
    rc, out, _ = _call_get_compose_invocation(
        "podman-compose", "cpu", str(tmp_path),
        extra_env={"VCT_STACK_COMPOSE_OVERRIDE": "compose.override.yaml"},
    )
    assert rc == 0
    assert out == "podman-compose -f compose.yaml -f compose.override.yaml"


def test_override_absent_no_flag(tmp_path: Path):
    rc, out, _ = _call_get_compose_invocation(
        "podman-compose", "cpu", str(tmp_path),
        extra_env={"VCT_STACK_COMPOSE_OVERRIDE": "compose.override.yaml"},
    )
    assert rc == 0
    assert out == "podman-compose -f compose.yaml"


def test_override_empty_file_no_flag(tmp_path: Path):
    (tmp_path / "compose.override.yaml").write_text("")
    rc, out, _ = _call_get_compose_invocation(
        "podman-compose", "cpu", str(tmp_path),
        extra_env={"VCT_STACK_COMPOSE_OVERRIDE": "compose.override.yaml"},
    )
    assert rc == 0
    assert out == "podman-compose -f compose.yaml"


def test_override_with_gpu_overlay_both_flags_override_last(tmp_path: Path):
    """When both the GPU overlay and the user override are present, both
    `-f` flags must appear AND the override must come LAST."""
    _materialize_overlays(tmp_path)
    _materialize_override(tmp_path)
    rc, out, _ = _call_get_compose_invocation(
        "podman-compose", "gpu", str(tmp_path),
        extra_env={"VCT_STACK_COMPOSE_OVERRIDE": "compose.override.yaml"},
    )
    assert rc == 0
    assert out == (
        "podman-compose -f compose.yaml "
        "-f infrastructure/podman-compose.gpu.yml "
        "-f compose.override.yaml"
    )
    overlay_idx = out.index("podman-compose.gpu.yml")
    override_idx = out.index("compose.override.yaml")
    assert override_idx > overlay_idx


def test_override_docker_runtime(tmp_path: Path):
    _materialize_override(tmp_path)
    rc, out, _ = _call_get_compose_invocation(
        "docker", "cpu", str(tmp_path),
        extra_env={"VCT_STACK_COMPOSE_OVERRIDE": "compose.override.yaml"},
    )
    assert rc == 0
    assert out == "docker compose -f compose.yaml -f compose.override.yaml"


def test_override_absolute_path(tmp_path: Path):
    """When the env var is an absolute path, the resolved path bypasses
    working_dir concatenation."""
    abs_override = tmp_path / "elsewhere" / "custom.override.yaml"
    abs_override.parent.mkdir()
    abs_override.write_text("services: {}\n")
    rc, out, _ = _call_get_compose_invocation(
        "podman-compose", "cpu", str(tmp_path),
        extra_env={"VCT_STACK_COMPOSE_OVERRIDE": str(abs_override)},
    )
    assert rc == 0
    # PS may render Windows-style or POSIX-style paths depending on the
    # host. Accept either by checking that the absolute path's basename
    # appears in the expected position.
    assert out.endswith(f"-f {abs_override}") or out.endswith(
        f"-f {str(abs_override).replace(chr(92), '/')}"
    ), out


# ---------------------------------------------------------------------------
# Sourcing must not trigger Invoke-Main (entrypoint guard test).
# ---------------------------------------------------------------------------


def test_sourcing_does_not_run_main(tmp_path: Path):
    """The guard at the bottom of the script (`$MyInvocation.InvocationName
    -ne '.'`) must skip Invoke-Main when dot-sourced. Otherwise the
    script would try to chdir to a non-existent working dir and exit 2,
    and would have written to the log file."""
    log = tmp_path / "should-not-be-written.log"
    proc = subprocess.run(
        [
            _PWSH, "-NoProfile", "-NonInteractive", "-Command",
            f"$env:VCT_STACK_LOG_FILE = {_ps_quote(str(log))}; "
            f". {_ps_quote(str(SCRIPT))}; "
            f"Write-Output 'SOURCED_OK'; exit 0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "SOURCED_OK" in proc.stdout
    assert not log.exists(), (
        f"sourcing wrote to log: {log.read_text() if log.exists() else '(none)'}"
    )
