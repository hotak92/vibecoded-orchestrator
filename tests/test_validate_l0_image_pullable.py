# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""E-3 — publisher-CI ordering gate: `validate-l0-image-pullable.sh`.

Enforces the strict paid-module publish order (GHCR push BEFORE the L0
catalog republish) by asserting that every image tag referenced by an L0
entry is pullable (E-findings §E-3). This suite exercises the script's
JSON-extraction and exit-code contract HERMETICALLY: a stub container
runtime on PATH stands in for docker/podman so no real registry is
touched (no GHCR mutation, per the E-3 brief).

Contract pinned here:
  * empty/absent install.container.image -> usage error (rc 2);
  * referenced image pullable (stub returns 0) -> rc 0;
  * referenced image NOT pullable (stub returns non-zero) -> rc 1;
  * --image mode bypasses JSON parsing;
  * the script never invokes a push/tag/build subcommand on the runtime.

All fixtures are synthetic — `example`-style registry refs, no secrets.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "docs" / "publisher-ci" / "validate-l0-image-pullable.sh"


def _make_stub_runtime(tmp_path: Path, *, pullable: bool) -> Path:
    """Create a fake `docker` on PATH.

    It logs every invocation to <bindir>/calls.log and returns success for
    `manifest inspect` / `pull` iff ``pullable``. Any push/tag/build call
    exits non-zero AND is recorded so the test can assert it never runs.
    """
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    log = bindir / "calls.log"
    inspect_pull_rc = 0 if pullable else 1
    script = f"""#!/usr/bin/env bash
echo "$@" >> "{log}"
case "$1" in
  manifest) exit {inspect_pull_rc} ;;
  pull)     exit {inspect_pull_rc} ;;
  push|tag|build) exit 3 ;;
  *)        exit 0 ;;
esac
"""
    stub = bindir / "docker"
    stub.write_text(script, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def _run_gate(
    *args: str, stub_bindir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": (f"{stub_bindir}:" if stub_bindir else "")
        + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "VCT_CONTAINER_RUNTIME": "docker",
    }
    return subprocess.run(
        ["sh", str(GATE), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _l0(tmp_path: Path, image: str) -> Path:
    p = tmp_path / "l0-entry.json"
    p.write_text(
        json.dumps({"install": {"container": {"image": image}}}),
        encoding="utf-8",
    )
    return p


def test_usage_error_no_args():
    r = _run_gate()
    assert r.returncode == 2, r.stderr


def test_empty_image_is_usage_error(tmp_path):
    l0 = _l0(tmp_path, "")
    r = _run_gate(str(l0))
    assert r.returncode == 2, r.stderr
    assert "install.container.image is empty" in r.stderr, r.stderr


def test_missing_l0_file_is_usage_error(tmp_path):
    r = _run_gate(str(tmp_path / "nope.json"))
    assert r.returncode == 2, r.stderr


def test_pullable_image_passes(tmp_path):
    stub = _make_stub_runtime(tmp_path, pullable=True)
    l0 = _l0(tmp_path, "ghcr.io/example/vct-mod:1.2.3-cpu")
    r = _run_gate(str(l0), stub_bindir=stub)
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "safe to republish" in r.stdout, r.stdout


def test_unpullable_image_fails_with_ordering_error(tmp_path):
    stub = _make_stub_runtime(tmp_path, pullable=False)
    l0 = _l0(tmp_path, "ghcr.io/example/vct-mod:1.2.3-cpu")
    r = _run_gate(str(l0), stub_bindir=stub)
    assert r.returncode == 1, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "not pullable" in r.stderr, r.stderr
    assert "republish the L0" in r.stderr, r.stderr


def test_image_mode_bypasses_json(tmp_path):
    stub = _make_stub_runtime(tmp_path, pullable=True)
    r = _run_gate(
        "--image",
        "ghcr.io/example/vct-mod:1.2.3-cpu",
        "ghcr.io/example/vct-mod:1.2.3-cuda",
        stub_bindir=stub,
    )
    assert r.returncode == 0, r.stderr
    # Both tags were checked.
    assert r.stdout.count("Checking pullability") == 2, r.stdout


def test_gate_never_pushes_or_tags(tmp_path):
    """The gate is READ-ONLY: it must never invoke push/tag/build on the
    runtime (those exit 3 in the stub AND are logged)."""
    stub = _make_stub_runtime(tmp_path, pullable=True)
    l0 = _l0(tmp_path, "ghcr.io/example/vct-mod:1.2.3-cpu")
    r = _run_gate(str(l0), stub_bindir=stub)
    assert r.returncode == 0, r.stderr
    calls = (stub / "calls.log").read_text(encoding="utf-8")
    for mutating in ("push", "tag", "build"):
        assert mutating not in calls, (
            f"gate invoked a mutating runtime subcommand: {calls!r}"
        )
    # It DID query pullability (manifest inspect or pull).
    assert ("manifest" in calls) or ("pull" in calls), calls
