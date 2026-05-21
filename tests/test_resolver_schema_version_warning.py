# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.23 D6 — resolver-client forward-compat warning parity.

The hub's ``GET /api/v1/projects/{id}/config`` response carries a
``schema_version`` integer (v0.2.22 Item #2). Three resolver clients
read this response and MUST emit a one-line stderr warning when the
hub reports a higher version than the client understands:

  * Python — ``vco_lib/project_config.py::_maybe_warn_schema_version``
  * Bash   — ``templates/scripts/vct_project_config.sh``
  * PowerShell — ``templates/scripts/vct_project_config.ps1``

The Python sibling already has dedicated coverage in
``test_project_config.py::SchemaVersionTest``. This file pins the
bash + ps1 siblings against a stub HTTP server that returns canned
JSON envelopes — same wire contract as the hub but no launcher
needed. Tests are sub-second and run on every Linux/macOS CI worker;
the ps1 subset gates on ``pwsh`` being on PATH (mirrors the pattern
in ``tests/test_launch_claude_mcp_stack_ps1.py``).
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
BASH_CLIENT = REPO_ROOT / "templates" / "scripts" / "vct_project_config.sh"
PS1_CLIENT = REPO_ROOT / "templates" / "scripts" / "vct_project_config.ps1"

# Prefer pwsh (cross-platform PowerShell Core); fall back to
# powershell.exe (Windows in-box PowerShell 5.1) when only that is
# available. Mirrors the pattern in test_launch_claude_mcp_stack_ps1.py.
_PWSH = shutil.which("pwsh") or shutil.which("powershell")


# ─── Stub hub HTTP server ─────────────────────────────────────────────────


def _minimal_config_body(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Minimal valid full-config envelope. Bash/ps1 clients only echo
    the body (they don't decode every field), so we keep the body small
    but well-formed JSON. `extra` overrides any field."""
    body: dict[str, Any] = {
        "project_id": "550e8400-e29b-41d4-a716-446655440000",
        "project_path": "/tmp/p",
        "project_slug": "p",
    }
    if extra:
        body.update(extra)
    return body


class _StubConfigHandler(http.server.BaseHTTPRequestHandler):
    """Serves a fixed JSON body on `GET /api/v1/projects/<id>/config`.

    The body is set per-test via `_StubConfigHandler.body = {...}`.
    """

    body: dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        # Accept any path under /api/v1/projects/.../config — the bash
        # script URL-encodes the project id, so an exact-string match
        # would be fragile.
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
        # Silence per-request stderr noise — would pollute the test's
        # stderr capture (the WHOLE POINT of these tests).
        pass


def _start_stub_hub(body: dict[str, Any]) -> tuple[http.server.HTTPServer, int]:
    """Start a localhost stub on an ephemeral port serving `body` on
    every config GET. Returns ``(server, port)``. The caller owns
    ``server.shutdown()``."""
    _StubConfigHandler.body = body
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubConfigHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Sanity-poll: confirm the listener is up before the test fires.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return server, port


# ─── Subprocess driver for the bash client ─────────────────────────────


def _run_bash_client(
    port: int,
    project_arg: str = "dummy-uuid-not-a-path",
    field: str | None = None,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``vct_project_config.sh <project>`` against a stub hub on
    ``port``. ``project_arg`` defaults to a path-less UUID-like string
    so the script skips ``by-path`` lookup and goes straight to the
    config fetch (which is what we want to exercise).

    The script discovers the hub via ``$VCT_HUB_PORT`` + ``$VCT_HUB_TOKEN``
    (the env-var branch of `hub_port`/`hub_token` — see the script).
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",
        "VCT_HUB_PORT": str(port),
        "VCT_HUB_TOKEN": "test-token",
        # Steer the rate-limited fall-through warning state file into a
        # tempdir so we don't pollute the user's $HOME/.vct/cache.
        "VCT_STATE_DIR": tempfile.mkdtemp(prefix="vct-resolver-test-"),
    }
    if extra_env:
        env.update(extra_env)
    argv = ["bash", str(BASH_CLIENT), project_arg]
    if field:
        argv.extend(["--field", field])
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=15
    )


# ─── Bash client tests ──────────────────────────────────────────────────


_WARNING_MARKER = "[vct_project_config] WARNING: hub schema_version="


class BashSchemaWarningTest(unittest.TestCase):
    """Pins the bash client's schema_version forward-compat warning."""

    def test_bash_client_warns_on_higher_hub_version(self) -> None:
        # Hub reports version 99 — much higher than RESOLVER_PROTOCOL_VERSION=1.
        # Script must emit exactly one warning line to stderr, must still
        # exit 0, and must print the full body to stdout.
        body = _minimal_config_body({"schema_version": 99})
        server, port = _start_stub_hub(body)
        try:
            result = _run_bash_client(port)
        finally:
            server.shutdown()

        self.assertEqual(
            result.returncode, 0,
            f"expected exit 0, got {result.returncode}\n"
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
        self.assertIn(_WARNING_MARKER, result.stderr,
            f"expected warning in stderr, got: {result.stderr!r}")
        self.assertIn("schema_version=99", result.stderr)
        self.assertIn("RESOLVER_PROTOCOL_VERSION=1", result.stderr)
        # Warning must fire EXACTLY ONCE per invocation (matches python
        # sibling's per-process contract — the bash script only fetches
        # once, so one occurrence is the natural ceiling).
        self.assertEqual(
            result.stderr.count(_WARNING_MARKER), 1,
            f"expected exactly one warning, stderr={result.stderr!r}"
        )

    def test_bash_client_no_warning_on_equal_version(self) -> None:
        # Hub reports version 1 — matches the client's constant. Silent.
        body = _minimal_config_body({"schema_version": 1})
        server, port = _start_stub_hub(body)
        try:
            result = _run_bash_client(port)
        finally:
            server.shutdown()

        self.assertEqual(result.returncode, 0,
            f"stderr={result.stderr!r}")
        self.assertNotIn(_WARNING_MARKER, result.stderr,
            f"unexpected warning for equal version: {result.stderr!r}")

    def test_bash_client_no_warning_on_missing_field(self) -> None:
        # Pre-v0.2.22 hub: no schema_version field at all. The client
        # must treat this as version 1 (silent back-fill) — no warning,
        # no crash.
        body = _minimal_config_body()
        self.assertNotIn("schema_version", body)
        server, port = _start_stub_hub(body)
        try:
            result = _run_bash_client(port)
        finally:
            server.shutdown()

        self.assertEqual(result.returncode, 0,
            f"unexpected non-zero exit for missing-field body; "
            f"stderr={result.stderr!r}")
        self.assertNotIn(_WARNING_MARKER, result.stderr,
            f"unexpected warning for missing field: {result.stderr!r}")

    def test_bash_client_no_warning_on_lower_version(self) -> None:
        # Hub reports an OLDER version (0) — silent (additive protocol;
        # older hubs just mean a smaller field set, which the client
        # already handles).
        body = _minimal_config_body({"schema_version": 0})
        server, port = _start_stub_hub(body)
        try:
            result = _run_bash_client(port)
        finally:
            server.shutdown()

        self.assertEqual(result.returncode, 0)
        self.assertNotIn(_WARNING_MARKER, result.stderr,
            f"unexpected warning for lower version: {result.stderr!r}")


# ─── PowerShell client tests ────────────────────────────────────────────


@unittest.skipIf(
    _PWSH is None,
    "no PowerShell runtime on PATH (pwsh / powershell.exe). "
    "PS1 resolver-warning tests skipped — install PowerShell Core 7+ "
    "to exercise this matrix on non-Windows hosts."
)
class PowerShellSchemaWarningTest(unittest.TestCase):
    """Pins the ps1 client's schema_version forward-compat warning."""

    def _run_ps1_client(
        self, port: int, project_arg: str = "dummy-uuid-not-a-path"
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",
            "VCT_HUB_PORT": str(port),
            "VCT_HUB_TOKEN": "test-token",
            "VCT_STATE_DIR": tempfile.mkdtemp(prefix="vct-resolver-test-"),
        }
        argv = [
            _PWSH, "-NoProfile", "-NonInteractive",
            "-File", str(PS1_CLIENT),
            "-Project", project_arg,
        ]
        return subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=30
        )

    def test_ps1_client_warns_on_higher_hub_version(self) -> None:
        body = _minimal_config_body({"schema_version": 99})
        server, port = _start_stub_hub(body)
        try:
            result = self._run_ps1_client(port)
        finally:
            server.shutdown()

        self.assertEqual(result.returncode, 0,
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}")
        self.assertIn(_WARNING_MARKER, result.stderr,
            f"expected warning in stderr, got: {result.stderr!r}")
        self.assertIn("schema_version=99", result.stderr)
        self.assertIn("RESOLVER_PROTOCOL_VERSION=1", result.stderr)
        self.assertEqual(
            result.stderr.count(_WARNING_MARKER), 1,
            f"expected exactly one warning, stderr={result.stderr!r}"
        )

    def test_ps1_client_no_warning_on_equal_version(self) -> None:
        body = _minimal_config_body({"schema_version": 1})
        server, port = _start_stub_hub(body)
        try:
            result = self._run_ps1_client(port)
        finally:
            server.shutdown()

        self.assertEqual(result.returncode, 0,
            f"stderr={result.stderr!r}")
        self.assertNotIn(_WARNING_MARKER, result.stderr,
            f"unexpected warning for equal version: {result.stderr!r}")

    def test_ps1_client_no_warning_on_missing_field(self) -> None:
        body = _minimal_config_body()
        self.assertNotIn("schema_version", body)
        server, port = _start_stub_hub(body)
        try:
            result = self._run_ps1_client(port)
        finally:
            server.shutdown()

        self.assertEqual(result.returncode, 0,
            f"unexpected non-zero exit: stderr={result.stderr!r}")
        self.assertNotIn(_WARNING_MARKER, result.stderr,
            f"unexpected warning for missing field: {result.stderr!r}")


if __name__ == "__main__":
    unittest.main()
