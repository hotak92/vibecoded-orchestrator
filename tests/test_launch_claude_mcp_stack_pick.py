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


def _call_pick(
    runtime: str,
    gpu_mode: str,
    working_dir: str | None = None,
) -> tuple[int, str, str]:
    """Source the wrapper script and call its pick_compose_invocation
    function. Returns (rc, stdout, stderr).

    `working_dir` is passed as the optional 3rd arg to the function (v0.2.10
    L1 — used for overlay existence check). When None, defaults to /tmp
    (which we assume has no `infrastructure/*.gpu.yml` files; tests that
    want the overlay-present branch supply their own tmp_path with the
    overlay materialised inside)."""
    # We deliberately set BASH_SOURCE[0] != $0 by sourcing in a non-main
    # context. `bash -c 'source FILE; pick_compose_invocation A B'`
    # has BASH_SOURCE[0]=FILE and $0=bash → main() is NOT invoked.
    wd = working_dir if working_dir is not None else "/tmp"
    cmd = [
        BASH or "bash",
        "-c",
        # Emit OVERLAY_MISSING_WARNED on stderr after the call so the test
        # can assert on the inline-GPU fall-through flag.
        (
            f'source "{SCRIPT}"; '
            'OVERLAY_MISSING_WARNED=0; '
            'pick_compose_invocation "$1" "$2" "$3"; rc=$?; '
            'printf "OVERLAY_MISSING_WARNED=%s\\n" '
            '"${OVERLAY_MISSING_WARNED:-0}" 1>&2; '
            'exit $rc'
        ),
        "_",  # $0
        runtime,
        gpu_mode,
        wd,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _overlay_warned(stderr: str) -> bool:
    """Parse the OVERLAY_MISSING_WARNED=<0|1> line emitted by _call_pick."""
    for line in stderr.splitlines():
        if line.startswith("OVERLAY_MISSING_WARNED="):
            return line.endswith("=1")
    return False


def _materialize_overlays(working_dir: Path) -> None:
    """Create empty-but-nonzero overlay files at the standard paths so
    `overlay_exists` returns true. Used by the gpu-with-overlay tests.

    v0.2.10 L1: pick_compose_invocation now checks overlay existence. To
    keep the original "gpu → -f overlay" expectations green we provision
    the overlay files in a tmp_path the test passes as `working_dir`."""
    infra = working_dir / "infrastructure"
    infra.mkdir(parents=True, exist_ok=True)
    (infra / "docker-compose.gpu.yml").write_text("# stub overlay\n")
    (infra / "podman-compose.gpu.yml").write_text("# stub overlay\n")


# ---------------------------------------------------------------------------
# docker × {gpu, cpu}
# ---------------------------------------------------------------------------


def test_docker_gpu_picks_docker_compose_with_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_pick("docker", "gpu", str(tmp_path))
    assert rc == 0
    assert out == (
        "docker compose -f compose.yaml -f infrastructure/docker-compose.gpu.yml"
    )
    assert not _overlay_warned(err)


def test_docker_cpu_picks_docker_compose_no_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_pick("docker", "cpu", str(tmp_path))
    assert rc == 0
    assert out == "docker compose -f compose.yaml"
    # cpu mode should never set the warning flag — even when overlay exists.
    assert not _overlay_warned(err)


# ---------------------------------------------------------------------------
# podman-compose × {gpu, cpu}
# ---------------------------------------------------------------------------


def test_podman_compose_gpu_picks_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_pick("podman-compose", "gpu", str(tmp_path))
    assert rc == 0
    assert out == (
        "podman-compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml"
    )
    assert not _overlay_warned(err)


def test_podman_compose_cpu_no_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_pick("podman-compose", "cpu", str(tmp_path))
    assert rc == 0
    assert out == "podman-compose -f compose.yaml"
    assert not _overlay_warned(err)


# ---------------------------------------------------------------------------
# podman compose (the subcommand form) × {gpu, cpu}
# ---------------------------------------------------------------------------


def test_podman_subcommand_gpu_picks_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_pick("podman compose", "gpu", str(tmp_path))
    assert rc == 0
    assert out == (
        "podman compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml"
    )
    assert not _overlay_warned(err)


def test_podman_subcommand_cpu_no_overlay(tmp_path: Path):
    _materialize_overlays(tmp_path)
    rc, out, err = _call_pick("podman compose", "cpu", str(tmp_path))
    assert rc == 0
    assert out == "podman compose -f compose.yaml"
    assert not _overlay_warned(err)


# ---------------------------------------------------------------------------
# v0.2.10 L1 — inline-GPU fall-through when the overlay file is missing.
# This is the canonical case for the user's Claude orchestrator stack at
# ~/Desktop/PROGETTI/Claude/claude_mcp_servers/compose.yaml, which has
# GPU devices declared inline (devices: - nvidia.com/gpu=all) on the
# ollama / code_embed service blocks. No overlay file exists there.
# ---------------------------------------------------------------------------


def test_podman_compose_gpu_missing_overlay_falls_through_to_inline(
    tmp_path: Path,
):
    # tmp_path is empty — no infrastructure/podman-compose.gpu.yml.
    rc, out, err = _call_pick("podman-compose", "gpu", str(tmp_path))
    assert rc == 0
    # gpu mode + overlay missing → emit WITHOUT -f overlay.
    assert out == "podman-compose -f compose.yaml"
    assert _overlay_warned(err), (
        "OVERLAY_MISSING_WARNED flag must be set when gpu_mode=gpu and "
        "the overlay file does not exist"
    )


def test_podman_subcommand_gpu_missing_overlay_falls_through(tmp_path: Path):
    rc, out, err = _call_pick("podman compose", "gpu", str(tmp_path))
    assert rc == 0
    assert out == "podman compose -f compose.yaml"
    assert _overlay_warned(err)


def test_docker_gpu_missing_overlay_falls_through(tmp_path: Path):
    rc, out, err = _call_pick("docker", "gpu", str(tmp_path))
    assert rc == 0
    assert out == "docker compose -f compose.yaml"
    assert _overlay_warned(err)


def test_cpu_mode_never_warns_even_when_overlay_missing(tmp_path: Path):
    """cpu mode is the no-overlay path by design; the missing-overlay
    branch only fires when gpu_mode=gpu AND the overlay isn't on disk."""
    rc, out, err = _call_pick("podman-compose", "cpu", str(tmp_path))
    assert rc == 0
    assert out == "podman-compose -f compose.yaml"
    assert not _overlay_warned(err)


# ---------------------------------------------------------------------------
# overlay_exists pure-function tests (path-as-argument, no env reads)
# ---------------------------------------------------------------------------


def _call_overlay_exists(path: str) -> int:
    cmd = [
        BASH or "bash",
        "-c",
        f'source "{SCRIPT}"; overlay_exists "$1"',
        "_",
        path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    return proc.returncode


def test_overlay_exists_returns_zero_for_existing_file(tmp_path: Path):
    f = tmp_path / "overlay.yml"
    f.write_text("contents\n")
    assert _call_overlay_exists(str(f)) == 0


def test_overlay_exists_returns_nonzero_for_missing_file(tmp_path: Path):
    assert _call_overlay_exists(str(tmp_path / "does-not-exist.yml")) == 1


def test_overlay_exists_returns_nonzero_for_empty_file(tmp_path: Path):
    f = tmp_path / "empty.yml"
    f.write_text("")
    assert _call_overlay_exists(str(f)) == 1


def test_overlay_exists_returns_nonzero_for_empty_arg():
    assert _call_overlay_exists("") == 1


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
    honoured. v0.2.10 L1: pick_compose_invocation now checks overlay
    existence, so we materialize the custom overlay path inside tmp_path
    and pass tmp_path as the working_dir argument."""
    custom = "custom/path.gpu.yml"
    (tmp_path / "custom").mkdir()
    (tmp_path / custom).write_text("# stub overlay\n")
    import os
    env = os.environ.copy()
    env["VCT_STACK_GPU_OVERLAY"] = custom
    env["VCT_STACK_GPU_OVERLAY_DOCKER"] = custom
    cmd = [
        BASH or "bash",
        "-c",
        f'source "{SCRIPT}"; pick_compose_invocation "$1" "$2" "$3"',
        "_",
        "podman-compose",
        "gpu",
        str(tmp_path),
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
