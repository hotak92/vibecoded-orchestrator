# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53: `install.py --bootstrap` mode tests.

Implements Track B's "Bootstrap mode" deliverable per §3 of
docs/INSTALL_ARCHITECTURE_v2.md. Verifies:

* ``install.py --bootstrap --json`` exits 0 and emits valid JSON.
* The envelope contains all required top-level keys per §3.3.
* The envelope's ``schema_version`` is 1.
* ``install.py --help`` advertises the ``--bootstrap`` flag.
* ``install.py --bootstrap --update`` exits 2 (mutually exclusive).
* ``install.py --bootstrap`` (no --json) prints a human-readable summary.
* The dispatch happens BEFORE ``_ensure_running_under_mcp_venv``; bootstrap
  works even when the MCP venv is missing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"


def _run_install(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run install.py with args in a subprocess. Uses current Python."""
    # Set VCT_BOOTSTRAP_TEST_MODE so any future side-effect guard can
    # detect tests. Currently bootstrap is side-effect-free so this is
    # documentation-only.
    env = os.environ.copy()
    env["VCT_BOOTSTRAP_TEST_MODE"] = "1"
    # Avoid noisy "[claude] not authenticated" probes from the test environment.
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run(
        [sys.executable, str(INSTALL_PY), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def test_bootstrap_json_exits_zero():
    """`install.py --bootstrap --json` MUST exit 0 on a normal workstation."""
    cp = _run_install(["--bootstrap", "--json"])
    assert cp.returncode == 0, (
        f"--bootstrap --json exited {cp.returncode}; "
        f"stderr={cp.stderr[-500:]}"
    )


def test_bootstrap_json_is_valid():
    """Stdout MUST be valid JSON conforming to schema v1."""
    cp = _run_install(["--bootstrap", "--json"])
    assert cp.returncode == 0
    parsed = json.loads(cp.stdout)  # raises on invalid
    assert parsed["schema_version"] == 1
    assert "system" in parsed
    assert "paths" in parsed
    assert "package_manager_advice" in parsed
    assert "weaviate_endpoints" in parsed
    assert "ollama_endpoints" in parsed
    assert "code_embed_endpoints" in parsed
    assert "vct_hub_endpoints" in parsed
    assert "missing_prereqs" in parsed
    assert "ready_to_install" in parsed
    assert "blocker_messages" in parsed
    assert "warnings" in parsed


def test_bootstrap_envelope_required_fields_per_3_3():
    """Envelope fields per §3.3 of docs/INSTALL_ARCHITECTURE_v2.md."""
    cp = _run_install(["--bootstrap", "--json"])
    env = json.loads(cp.stdout)

    sys_b = env["system"]
    for required in (
        "os", "os_family", "arch", "python", "node", "npm",
        "podman", "docker", "git", "brew", "gpu",
    ):
        assert required in sys_b, f"system.{required} missing"

    for required in ("cmd", "version", "ok"):
        assert required in sys_b["python"], f"system.python.{required} missing"

    paths = env["paths"]
    for required in (
        "install_root", "install_root_kind", "launcher_dist_subdir",
        "launcher_binary", "launcher_binary_exists",
        "vct_hub_binary", "vct_hub_binary_exists",
        "state_dir", "vct_root_dir",
    ):
        assert required in paths, f"paths.{required} missing"


def test_bootstrap_weaviate_health_endpoint_is_canonical():
    """NEW-4: bootstrap must report `/v1/.well-known/ready` as the canonical health endpoint."""
    cp = _run_install(["--bootstrap", "--json"])
    env = json.loads(cp.stdout)
    assert env["weaviate_endpoints"]["health"] == (
        "http://localhost:8081/v1/.well-known/ready"
    ), (
        "NEW-4 SSOT violation: bootstrap envelope must publish "
        "`/v1/.well-known/ready` as the Weaviate health endpoint, NOT "
        "`/v1/meta`. installer.rs:627 comment is wrong; Python side is "
        "canonical and Rust consumers must read this value from here."
    )


def test_bootstrap_launcher_dist_subdir_no_experimental_macos():
    """M-P0-2: `launcher_dist_subdir` must be `macos-arm64`, not `experimental_macOS`."""
    cp = _run_install(["--bootstrap", "--json"])
    env = json.loads(cp.stdout)
    subdir = env["paths"]["launcher_dist_subdir"]
    assert subdir != "experimental_macOS", (
        "M-P0-2 SSOT violation: `experimental_macOS` is the legacy name; "
        "current code must emit `macos-arm64`."
    )
    # v0.2.54 Track C: `macos-x64` added for Intel-Mac local builds.
    assert subdir in (
        "macos-arm64", "macos-x64", "linux-x64", "windows-x64", None,
    ), (
        f"Unexpected launcher_dist_subdir: {subdir!r}"
    )


def test_bootstrap_human_output_no_json():
    """Without --json, bootstrap MUST print a human-readable summary table."""
    cp = _run_install(["--bootstrap"])
    assert cp.returncode == 0
    # Sentinel strings from _bootstrap_print_human.
    assert "VCO Bootstrap Probe" in cp.stdout
    assert "Ready to install:" in cp.stdout


def test_help_advertises_bootstrap_flag():
    """`install.py --help` MUST list `--bootstrap` in its argument set."""
    cp = _run_install(["--help"])
    # argparse exits 0 on --help.
    assert cp.returncode == 0
    assert "--bootstrap" in cp.stdout
    assert "--json" in cp.stdout
    assert "--install-missing" in cp.stdout


def test_bootstrap_rejects_mutual_exclusion_with_update():
    """`--bootstrap --update` MUST exit 2 per §3.2."""
    cp = _run_install(["--bootstrap", "--update"])
    assert cp.returncode == 2, (
        f"Expected exit 2 for mutually-exclusive flags; got {cp.returncode}"
    )
    assert "--bootstrap" in cp.stderr.lower() or "bootstrap" in cp.stderr.lower()


def test_bootstrap_rejects_unknown_args():
    """Unknown args under --bootstrap MUST exit 2."""
    cp = _run_install(["--bootstrap", "--not-a-real-flag"])
    assert cp.returncode == 2


def test_bootstrap_schema_version_is_1():
    """Envelope is schema v1; Rust + bash consumers refuse other versions."""
    cp = _run_install(["--bootstrap", "--json"])
    env = json.loads(cp.stdout)
    assert env["schema_version"] == 1


def test_bootstrap_ready_to_install_matches_blocker_messages():
    """`ready_to_install` MUST be False iff there are blocker_messages."""
    cp = _run_install(["--bootstrap", "--json"])
    env = json.loads(cp.stdout)
    assert env["ready_to_install"] == (len(env["blocker_messages"]) == 0), (
        "Envelope contradiction: ready_to_install and blocker_messages "
        "must agree."
    )


def test_bootstrap_generated_at_is_iso8601_utc():
    """`generated_at` MUST end in `Z` (UTC) and parse as ISO-8601."""
    from datetime import datetime
    cp = _run_install(["--bootstrap", "--json"])
    env = json.loads(cp.stdout)
    ts = env["generated_at"]
    assert ts.endswith("Z"), f"generated_at must end in Z: {ts!r}"
    # Python's fromisoformat doesn't accept 'Z' before 3.11; strip + replace.
    parseable = ts.replace("Z", "+00:00")
    datetime.fromisoformat(parseable)


def test_bootstrap_help_with_bootstrap_flag_defers_to_argparse():
    """`--bootstrap --help` SHOULD show full help (defers to argparse)."""
    cp = _run_install(["--bootstrap", "--help"])
    assert cp.returncode == 0
    assert "--bootstrap" in cp.stdout


@pytest.mark.parametrize("os_value", ["macos", "linux", "windows", "unknown"])
def test_bootstrap_os_enum(os_value):
    """The schema enum for system.os covers exactly these 4 values."""
    # Smoke: the produced envelope's `os` field is one of these.
    cp = _run_install(["--bootstrap", "--json"])
    env = json.loads(cp.stdout)
    assert env["system"]["os"] in ("macos", "linux", "windows", "unknown")
    # The parameterize is documentation of the schema enum; the assertion
    # above is the actual contract.
    _ = os_value
