# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.24 A4 — schema-version drift warning is rate-limited across invocations.

Before A4, ``vct_project_config.sh`` and ``.ps1`` used a process-local
one-shot flag (``_VCT_SCHEMA_WARNED`` / ``$Script:SchemaWarned``). Since
each script invocation is its own process, hooks that call the resolver
100+ times per Claude Code session were emitting the same schema-drift
warning 100+ times to stderr.

A4 routes the warning through the existing JSONL-backed rate-limit
infrastructure (``_emit_warning`` / ``Emit-Warning``) with a stable
cross-PID suppression key ``schema_version_drift_<hub_version>`` and a
5-minute window. ``VCO_HOOK_DEBUG=1`` bypasses suppression and emits on
every occurrence.

This test pins:

  * Two consecutive bash invocations against a drifted hub → only the
    FIRST writes the warning to stderr.
  * Same scenario with ``VCO_HOOK_DEBUG=1`` → BOTH invocations emit.
  * The JSONL row recorded in ``$VCT_STATE_DIR/cache/resolver_warn.jsonl``
    carries the expected ``schema_version_drift_<v>`` suppression key
    and shape.
  * Different observed hub versions are NOT cross-suppressed (each one
    earns its own 5-min window).

The ps1 side gates on ``pwsh`` being on PATH (matches the parity test
in ``test_resolver_schema_version_warning.py``).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
BASH_CLIENT = REPO_ROOT / "templates" / "scripts" / "vct_project_config.sh"
PS1_CLIENT = REPO_ROOT / "templates" / "scripts" / "vct_project_config.ps1"

_PWSH = shutil.which("pwsh") or shutil.which("powershell")

_WARNING_MARKER = "[vct_project_config] WARNING: hub schema_version="


# ─── Stub hub HTTP server (shared shape with test_resolver_schema_version_warning) ──


class _StubConfigHandler(http.server.BaseHTTPRequestHandler):
    body: dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802
        if "/api/v1/projects/" in self.path and "/config" in self.path:
            payload = json.dumps(self.__class__.body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: Any, **kwargs: Any) -> None:
        pass


def _start_stub_hub(body: dict[str, Any]) -> tuple[http.server.HTTPServer, int]:
    _StubConfigHandler.body = body
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubConfigHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return server, port


def _run_bash_client(
    port: int,
    state_dir: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the bash client against the stub hub.

    State directory is supplied by the caller so multiple invocations
    can share suppression state across processes (the whole point of
    this test).
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",
        "VCT_HUB_PORT": str(port),
        "VCT_HUB_TOKEN": "test-token",
        "VCT_STATE_DIR": state_dir,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(BASH_CLIENT), "dummy-uuid-not-a-path"],
        env=env, capture_output=True, text=True, timeout=15,
    )


def _jsonl_path(state_dir: str) -> Path:
    return Path(state_dir) / "cache" / "resolver_warn.jsonl"


# ─── Bash client tests ──────────────────────────────────────────────────


class BashSchemaWarningRateLimitTest(unittest.TestCase):
    """v0.2.24 A4: schema-version drift suppression survives across PIDs."""

    def setUp(self) -> None:
        self._state_dir = tempfile.mkdtemp(prefix="vct-a4-ratelimit-")

    def tearDown(self) -> None:
        shutil.rmtree(self._state_dir, ignore_errors=True)

    def test_second_invocation_within_window_is_suppressed(self) -> None:
        """Two back-to-back invocations against a drifted hub → only one warning."""
        body = {"project_id": "p", "project_path": "/tmp/p", "project_slug": "p",
                "schema_version": 99}
        server, port = _start_stub_hub(body)
        try:
            r1 = _run_bash_client(port, self._state_dir)
            r2 = _run_bash_client(port, self._state_dir)
        finally:
            server.shutdown()

        self.assertEqual(r1.returncode, 0, f"r1 stderr={r1.stderr!r}")
        self.assertEqual(r2.returncode, 0, f"r2 stderr={r2.stderr!r}")
        # First invocation: warning fires.
        self.assertIn(_WARNING_MARKER, r1.stderr,
            f"first call should warn, stderr={r1.stderr!r}")
        # Second invocation: suppressed (different PID, but same
        # suppression key schema_version_drift_99 keeps the warning silent).
        self.assertNotIn(_WARNING_MARKER, r2.stderr,
            f"second call should be suppressed, stderr={r2.stderr!r}")

    def test_debug_env_bypasses_suppression(self) -> None:
        """``VCO_HOOK_DEBUG=1`` → both calls emit, even back-to-back."""
        body = {"project_id": "p", "project_path": "/tmp/p", "project_slug": "p",
                "schema_version": 99}
        server, port = _start_stub_hub(body)
        try:
            debug_env = {"VCO_HOOK_DEBUG": "1"}
            r1 = _run_bash_client(port, self._state_dir, extra_env=debug_env)
            r2 = _run_bash_client(port, self._state_dir, extra_env=debug_env)
        finally:
            server.shutdown()

        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        self.assertIn(_WARNING_MARKER, r1.stderr)
        self.assertIn(_WARNING_MARKER, r2.stderr,
            f"VCO_HOOK_DEBUG=1 should bypass suppression, "
            f"stderr={r2.stderr!r}")

    def test_jsonl_row_carries_expected_suppression_key(self) -> None:
        """Recorded JSONL row uses ``schema_version_drift_<v>`` as key."""
        body = {"project_id": "p", "project_path": "/tmp/p", "project_slug": "p",
                "schema_version": 42}
        server, port = _start_stub_hub(body)
        try:
            result = _run_bash_client(port, self._state_dir)
        finally:
            server.shutdown()

        self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
        jsonl = _jsonl_path(self._state_dir)
        self.assertTrue(jsonl.exists(),
            f"expected JSONL row at {jsonl}, but file missing. "
            f"stderr={result.stderr!r}")
        lines = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        # Find the schema-drift row (other warnings may have been
        # appended too, e.g. if the run also triggered hub_unreachable
        # somewhere — we only care that ours is present).
        drift_rows = []
        for ln in lines:
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if row.get("error_kind") == "schema_version_drift":
                drift_rows.append(row)
        self.assertEqual(len(drift_rows), 1,
            f"expected one drift row, got {len(drift_rows)}; lines={lines!r}")
        row = drift_rows[0]
        self.assertEqual(row["key"], "schema_version_drift_42")
        self.assertIn("hub_version=42", row["detail"])
        self.assertIn("client_version=1", row["detail"])

    def test_different_hub_versions_are_not_cross_suppressed(self) -> None:
        """A drift from v=99 must NOT suppress a later drift from v=100."""
        body99 = {"project_id": "p", "project_path": "/tmp/p", "project_slug": "p",
                  "schema_version": 99}
        body100 = {"project_id": "p", "project_path": "/tmp/p", "project_slug": "p",
                   "schema_version": 100}
        server, port = _start_stub_hub(body99)
        try:
            r1 = _run_bash_client(port, self._state_dir)
            # Same hub, different version observed:
            _StubConfigHandler.body = body100
            r2 = _run_bash_client(port, self._state_dir)
        finally:
            server.shutdown()

        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        self.assertIn("schema_version=99", r1.stderr)
        # v=100 is a different drift class — emit again.
        self.assertIn("schema_version=100", r2.stderr,
            f"different hub version should NOT be cross-suppressed, "
            f"stderr={r2.stderr!r}")


# ─── PowerShell client tests ────────────────────────────────────────────


@unittest.skipIf(
    _PWSH is None,
    "no PowerShell runtime on PATH (pwsh / powershell.exe). "
    "PS1 rate-limit tests skipped — install PowerShell Core 7+ to run."
)
class PowerShellSchemaWarningRateLimitTest(unittest.TestCase):

    def setUp(self) -> None:
        self._state_dir = tempfile.mkdtemp(prefix="vct-a4-ratelimit-ps1-")

    def tearDown(self) -> None:
        shutil.rmtree(self._state_dir, ignore_errors=True)

    def _run_ps1(
        self, port: int, *, extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",
            "VCT_HUB_PORT": str(port),
            "VCT_HUB_TOKEN": "test-token",
            "VCT_STATE_DIR": self._state_dir,
        }
        if extra_env:
            env.update(extra_env)
        argv = [_PWSH, "-NoProfile", "-NonInteractive", "-File",
                str(PS1_CLIENT), "-Project", "dummy-uuid-not-a-path"]
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=30)

    def test_ps1_second_invocation_within_window_is_suppressed(self) -> None:
        body = {"project_id": "p", "project_path": "/tmp/p", "project_slug": "p",
                "schema_version": 99}
        server, port = _start_stub_hub(body)
        try:
            r1 = self._run_ps1(port)
            r2 = self._run_ps1(port)
        finally:
            server.shutdown()

        self.assertEqual(r1.returncode, 0, f"r1 stderr={r1.stderr!r}")
        self.assertEqual(r2.returncode, 0, f"r2 stderr={r2.stderr!r}")
        self.assertIn(_WARNING_MARKER, r1.stderr)
        self.assertNotIn(_WARNING_MARKER, r2.stderr,
            f"ps1 second call should be suppressed, stderr={r2.stderr!r}")

    def test_ps1_debug_env_bypasses_suppression(self) -> None:
        body = {"project_id": "p", "project_path": "/tmp/p", "project_slug": "p",
                "schema_version": 99}
        server, port = _start_stub_hub(body)
        try:
            debug_env = {"VCO_HOOK_DEBUG": "1"}
            r1 = self._run_ps1(port, extra_env=debug_env)
            r2 = self._run_ps1(port, extra_env=debug_env)
        finally:
            server.shutdown()

        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        self.assertIn(_WARNING_MARKER, r1.stderr)
        self.assertIn(_WARNING_MARKER, r2.stderr,
            f"VCO_HOOK_DEBUG=1 should bypass ps1 suppression, "
            f"stderr={r2.stderr!r}")

    def test_ps1_jsonl_row_carries_expected_suppression_key(self) -> None:
        body = {"project_id": "p", "project_path": "/tmp/p", "project_slug": "p",
                "schema_version": 42}
        server, port = _start_stub_hub(body)
        try:
            result = self._run_ps1(port)
        finally:
            server.shutdown()

        self.assertEqual(result.returncode, 0, f"stderr={result.stderr!r}")
        jsonl = _jsonl_path(self._state_dir)
        self.assertTrue(jsonl.exists(), f"expected JSONL at {jsonl}")
        lines = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        drift_rows = []
        for ln in lines:
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if row.get("error_kind") == "schema_version_drift":
                drift_rows.append(row)
        self.assertEqual(len(drift_rows), 1,
            f"expected one drift row, lines={lines!r}")
        self.assertEqual(drift_rows[0]["key"], "schema_version_drift_42")


if __name__ == "__main__":
    unittest.main()
