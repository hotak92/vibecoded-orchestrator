# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-D item 4 — stale-env hub-token fallback in the file-store CLI.

``tools/vct-secrets/vct`` reads ONLY the file store, but on a MISS it
probes vct-hub so the diagnostic can be honest ("the launcher keychain
HAS this key — resolve it, do NOT `vct set` a divergent copy"). With a
stale exported ``VCT_HUB_TOKEN`` that probe was refused (401/403) and
degraded to the weaker "could not confirm" message on a machine whose
on-disk ``hub.token`` would have answered definitively.

Pinned here (real subprocess against a stub hub, no mocks):
  * stale pin + fresh on-disk token → ONE retry → the CONFIRMED-HIT
    message + the definitive stderr line;
  * ``VCT_HUB_TOKEN_STRICT=1`` → the refusal classification stands;
  * no on-disk token → the refusal classification stands.

The `vct` bash suite (``tools/vct-secrets/tests/test_vct.sh``) has no CI
runner, so this coverage lives in pytest where the gate actually runs.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VCT = REPO_ROOT / "tools" / "vct-secrets" / "vct"

ENV_TOKEN = "stale-env-token-v0291-not-a-real-secret"
DISK_TOKEN = "fresh-disk-token-v0291-not-a-real-secret"
DEFINITIVE_LINE = "stale VCT_HUB_TOKEN in env overridden by on-disk hub.token"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("curl") is None,
    reason="vct is a bash script and its hub probe needs curl",
)


class _AuthHub:
    """Stub hub: 401 unless the bearer matches; records every bearer."""

    def __init__(self, expected: str):
        self.expected = expected
        self.bearers: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a, **kw):  # noqa: D102
                pass

            def do_GET(self):  # noqa: N802
                auth = self.headers.get("Authorization", "")
                parts = auth.split(None, 1)
                bearer = parts[1].strip() if len(parts) == 2 else ""
                outer.bearers.append(bearer)
                if bearer != outer.expected:
                    body = json.dumps(
                        {"error": {"code": "unauthorized", "message": "bad token"}}
                    ).encode()
                    status = 401
                else:
                    body = json.dumps({"SOMEKEY": "synthetic-keychain-value"}).encode()
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def hub():
    h = _AuthHub(DISK_TOKEN)
    try:
        yield h
    finally:
        h.stop()


def _run_vct_miss(tmp_path: Path, hub_port: int, *, disk_token: bool,
                  strict: bool) -> subprocess.CompletedProcess:
    """`vct get` on a key that is NOT in the file store → die_miss →
    the hub probe under test."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    if disk_token:
        (state / "hub.token").write_text(DISK_TOKEN, encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "VCT_SECRETS_DIR": str(tmp_path / "store"),
        "VCT_STATE_DIR": str(state),
        "VCT_HUB_PORT": str(hub_port),
        "VCT_HUB_TOKEN": ENV_TOKEN,
    })
    env.pop("VCT_HUB_TOKEN_STRICT", None)
    if strict:
        env["VCT_HUB_TOKEN_STRICT"] = "1"
    return subprocess.run(
        ["bash", str(VCT), "get", "--project", "synthetic-proj", "--key", "SOMEKEY"],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_stale_pin_retry_makes_the_miss_message_definitive(tmp_path, hub):
    """RED-PROOF: pre-fix the probe presented the stale token, got 401,
    classified the key state as UNKNOWN, and never emitted a fix."""
    cp = _run_vct_miss(tmp_path, hub.port, disk_token=True, strict=False)
    assert cp.returncode != 0, "the file-store miss itself still fails"
    assert "the launcher keychain HAS" in cp.stderr, (
        f"expected the CONFIRMED-HIT miss message; got: {cp.stderr}"
    )
    assert DEFINITIVE_LINE in cp.stderr, cp.stderr
    assert hub.bearers == [ENV_TOKEN, DISK_TOKEN], hub.bearers
    # A probe must never print a secret VALUE or a token.
    assert "synthetic-keychain-value" not in cp.stderr
    assert DISK_TOKEN not in cp.stderr and ENV_TOKEN not in cp.stderr


def test_strict_guard_keeps_the_refused_classification(tmp_path, hub):
    cp = _run_vct_miss(tmp_path, hub.port, disk_token=True, strict=True)
    assert cp.returncode != 0
    assert "could not confirm" in cp.stderr, cp.stderr
    assert DEFINITIVE_LINE not in cp.stderr
    assert hub.bearers == [ENV_TOKEN], hub.bearers


def test_no_on_disk_token_keeps_the_refused_classification(tmp_path, hub):
    cp = _run_vct_miss(tmp_path, hub.port, disk_token=False, strict=False)
    assert cp.returncode != 0
    assert "could not confirm" in cp.stderr, cp.stderr
    assert DEFINITIVE_LINE not in cp.stderr
    assert hub.bearers == [ENV_TOKEN], hub.bearers
