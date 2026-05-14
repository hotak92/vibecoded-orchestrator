"""Tests for the `pick_compose_invocation` bash function in
`scripts/launch-claude-mcp-stack.sh` (v0.2.9 Bug J).

We exercise the pure decision helper from Python by sourcing the script
in a subshell and calling the function with controlled arguments. The
script itself only runs `main` when invoked as `${0}`, so sourcing it
is a side-effect-free way to access the helper.

Matrix:
  runtime  ∈ "docker" | "podman-compose" | "podman compose" | "" | "fake"
  gpu_mode ∈ "gpu" | "cpu"

Expected output for each cell is pinned below. The wrapper script's
soft-fail behaviour for empty / unknown runtimes is also covered.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="bash not available in test environment (script is Linux/macOS only)",
)


def _call_pick(runtime: str, gpu_mode: str) -> tuple[int, str, str]:
    """Source the wrapper script and call its pick_compose_invocation
    function. Returns (rc, stdout, stderr)."""
    # We deliberately set BASH_SOURCE[0] != $0 by sourcing in a non-main
    # context. `bash -c 'source FILE; pick_compose_invocation A B'`
    # has BASH_SOURCE[0]=FILE and $0=bash → main() is NOT invoked.
    cmd = [
        BASH or "bash",
        "-c",
        f'source "{SCRIPT}"; pick_compose_invocation "$1" "$2"',
        "_",  # $0
        runtime,
        gpu_mode,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# ---------------------------------------------------------------------------
# docker × {gpu, cpu}
# ---------------------------------------------------------------------------


def test_docker_gpu_picks_docker_compose_with_overlay():
    rc, out, _ = _call_pick("docker", "gpu")
    assert rc == 0
    assert out == (
        "docker compose -f compose.yaml -f infrastructure/docker-compose.gpu.yml"
    )


def test_docker_cpu_picks_docker_compose_no_overlay():
    rc, out, _ = _call_pick("docker", "cpu")
    assert rc == 0
    assert out == "docker compose -f compose.yaml"


# ---------------------------------------------------------------------------
# podman-compose × {gpu, cpu}
# ---------------------------------------------------------------------------


def test_podman_compose_gpu_picks_overlay():
    rc, out, _ = _call_pick("podman-compose", "gpu")
    assert rc == 0
    assert out == (
        "podman-compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml"
    )


def test_podman_compose_cpu_no_overlay():
    rc, out, _ = _call_pick("podman-compose", "cpu")
    assert rc == 0
    assert out == "podman-compose -f compose.yaml"


# ---------------------------------------------------------------------------
# podman compose (the subcommand form) × {gpu, cpu}
# ---------------------------------------------------------------------------


def test_podman_subcommand_gpu_picks_overlay():
    rc, out, _ = _call_pick("podman compose", "gpu")
    assert rc == 0
    assert out == (
        "podman compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml"
    )


def test_podman_subcommand_cpu_no_overlay():
    rc, out, _ = _call_pick("podman compose", "cpu")
    assert rc == 0
    assert out == "podman compose -f compose.yaml"


# ---------------------------------------------------------------------------
# Soft-fail cases
# ---------------------------------------------------------------------------


def test_empty_runtime_returns_nonzero():
    rc, out, _ = _call_pick("", "gpu")
    assert rc == 1
    assert out == ""


def test_unknown_runtime_returns_nonzero():
    rc, out, _ = _call_pick("docker-fake", "gpu")
    assert rc == 2
    assert out == ""


# ---------------------------------------------------------------------------
# Overlay paths can be overridden via env — important for cross-OS testing
# and for users with non-default compose layouts.
# ---------------------------------------------------------------------------


def test_overlay_paths_override_via_env(tmp_path: Path):
    """Verify VCT_STACK_GPU_OVERLAY / VCT_STACK_GPU_OVERLAY_DOCKER are
    honoured. We just check the substituted path lands in the output —
    file existence is not required because pick_compose_invocation is
    pure (it doesn't touch disk)."""
    custom = "custom/path.gpu.yml"
    import os
    env = os.environ.copy()
    env["VCT_STACK_GPU_OVERLAY"] = custom
    env["VCT_STACK_GPU_OVERLAY_DOCKER"] = custom
    cmd = [
        BASH or "bash",
        "-c",
        f'source "{SCRIPT}"; pick_compose_invocation "$1" "$2"',
        "_",
        "podman-compose",
        "gpu",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
    assert proc.returncode == 0
    assert custom in proc.stdout


# ---------------------------------------------------------------------------
# Script structure smoke test — sourcing must not trigger main().
# ---------------------------------------------------------------------------


def test_sourcing_does_not_run_main(tmp_path: Path):
    """Verifies the `BASH_SOURCE[0] == ${0}` guard at the bottom of the
    script. If main() ran on source, it would chdir to a missing
    working directory and exit non-zero — and pollute the log file."""
    log = tmp_path / "should-not-be-written.log"
    cmd = [
        BASH or "bash",
        "-c",
        f'VCT_STACK_LOG_FILE="{log}" source "{SCRIPT}"; echo SOURCED_OK',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert "SOURCED_OK" in proc.stdout
    # Log file MUST NOT have been touched — main() would have written
    # the "starting" line on entry.
    assert not log.exists(), f"sourcing wrote to log: {log.read_text()}"
