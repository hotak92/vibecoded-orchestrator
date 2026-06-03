# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``claude_mcp_servers.rl_client.hub_writer.post_rl_event``.

The client is a small soft-fail wrapper around ``urllib.request``. Tests
exercise: token/port discovery, success path, every error class
(missing token, connect-refused, non-2xx, malformed body), and the
"hub not running" graceful-fail path.

A minimal ``http.server.HTTPServer`` runs in a background thread for
the success tests — same pattern as the existing
`tests/test_telemetry_orchestrator_v0231.py` uses to avoid taking on
a network-mock library dependency.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_mcp_servers.rl_client import hub_writer


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _RecordingHandler(BaseHTTPRequestHandler):
    """Stub hub. Echoes received requests into a class-level list."""

    requests: list[dict] = []
    response_status: int = 200
    response_body: bytes = b'{"ok":true,"id":1}'

    def do_POST(self) -> None:  # noqa: N802 — required name
        length = int(self.headers.get("content-length", "0"))
        body_raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(body_raw.decode("utf-8"))
        except json.JSONDecodeError:
            body = {"__decode_error": True, "raw": body_raw.decode("utf-8", errors="replace")}

        _RecordingHandler.requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("authorization", ""),
                "content_type": self.headers.get("content-type", ""),
                "body": body,
            }
        )
        self.send_response(_RecordingHandler.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_RecordingHandler.response_body)))
        self.end_headers()
        self.wfile.write(_RecordingHandler.response_body)

    def log_message(self, *args, **kwargs) -> None:  # silence stderr
        pass


def _start_stub_hub() -> tuple[HTTPServer, int]:
    """Bring up a stub hub on a random local port. Returns (server, port)."""
    _RecordingHandler.requests = []
    _RecordingHandler.response_status = 200
    _RecordingHandler.response_body = b'{"ok":true,"id":1}'
    server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


# ----------------------------------------------------------------------
# 1. Discovery helpers
# ----------------------------------------------------------------------


class TestVctRootResolution:
    def test_env_var_takes_precedence(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"VCT_STATE_DIR": str(tmp_path)}, clear=False):
            assert hub_writer._vct_root_dir() == tmp_path

    def test_defaults_to_home_dot_vct(self) -> None:
        env = dict(os.environ)
        env.pop("VCT_STATE_DIR", None)
        with patch.dict(os.environ, env, clear=True):
            assert hub_writer._vct_root_dir() == Path.home() / ".vct"


class TestPortResolution:
    def test_env_var_takes_precedence(self, tmp_path: Path) -> None:
        with patch.dict(
            os.environ,
            {"VCT_STATE_DIR": str(tmp_path), "VCT_HUB_PORT": "9999"},
            clear=False,
        ):
            assert hub_writer._read_hub_port() == 9999

    def test_falls_back_to_port_file(self, tmp_path: Path) -> None:
        (tmp_path / "hub.port").write_text("7755\n")
        env = dict(os.environ)
        env.pop("VCT_HUB_PORT", None)
        env["VCT_STATE_DIR"] = str(tmp_path)
        with patch.dict(os.environ, env, clear=True):
            assert hub_writer._read_hub_port() == 7755

    def test_default_when_nothing_set(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        env.pop("VCT_HUB_PORT", None)
        env["VCT_STATE_DIR"] = str(tmp_path)
        with patch.dict(os.environ, env, clear=True):
            # No hub.port file in tmp_path -> default 7700.
            assert hub_writer._read_hub_port() == 7700

    def test_invalid_env_falls_through(self, tmp_path: Path) -> None:
        (tmp_path / "hub.port").write_text("8001")
        with patch.dict(
            os.environ,
            {"VCT_STATE_DIR": str(tmp_path), "VCT_HUB_PORT": "not-a-number"},
            clear=False,
        ):
            # Bad env -> falls through to hub.port file.
            assert hub_writer._read_hub_port() == 8001


class TestTokenResolution:
    def test_reads_token_file(self, tmp_path: Path) -> None:
        (tmp_path / "hub.token").write_text("secret-token-abc\n")
        with patch.dict(os.environ, {"VCT_STATE_DIR": str(tmp_path)}, clear=False):
            assert hub_writer._read_hub_token() == "secret-token-abc"

    def test_missing_token_returns_none(self, tmp_path: Path) -> None:
        # No hub.token file in tmp_path.
        with patch.dict(os.environ, {"VCT_STATE_DIR": str(tmp_path)}, clear=False):
            assert hub_writer._read_hub_token() is None


# ----------------------------------------------------------------------
# 2. POST success path (against stub hub).
# ----------------------------------------------------------------------


class TestPostSuccessPath:
    def test_2xx_returns_true(self, tmp_path: Path) -> None:
        server, port = _start_stub_hub()
        try:
            (tmp_path / "hub.token").write_text("secret")
            (tmp_path / "hub.port").write_text(str(port))
            env = dict(os.environ)
            env.pop("VCT_HUB_PORT", None)
            env["VCT_STATE_DIR"] = str(tmp_path)
            with patch.dict(os.environ, env, clear=True):
                ok = hub_writer.post_rl_event(
                    {
                        "event_type": "retrieval",
                        "schema_version": 3,
                        "ts_ms": 1_700_000_000_000,
                        "task_id": "abc",
                        "payload_json": "{}",
                    }
                )
            assert ok is True
            assert len(_RecordingHandler.requests) == 1
            rec = _RecordingHandler.requests[0]
            assert rec["path"] == "/api/v1/rl/events"
            assert rec["auth"] == "Bearer secret"
            assert rec["content_type"] == "application/json"
            assert rec["body"]["task_id"] == "abc"
            assert rec["body"]["event_type"] == "retrieval"
        finally:
            server.shutdown()
            server.server_close()

    def test_passes_full_v3_payload_through(self, tmp_path: Path) -> None:
        server, port = _start_stub_hub()
        try:
            (tmp_path / "hub.token").write_text("tok")
            (tmp_path / "hub.port").write_text(str(port))
            env = dict(os.environ)
            env.pop("VCT_HUB_PORT", None)
            env["VCT_STATE_DIR"] = str(tmp_path)
            with patch.dict(os.environ, env, clear=True):
                event = {
                    "event_type": "citation",
                    "schema_version": 3,
                    "ts_ms": 1_700_000_000_001,
                    "project_id": None,
                    "project_name": "VCO_dev",
                    "task_id": "task-citation-1",
                    "task_type": "mcp_interactive",
                    "embedding_source": "qwen3",
                    "embedding_dim": 1024,
                    "embedding_model": "qwen3-embedding:0.6b",
                    "payload_json": json.dumps(
                        {
                            "event": "citation",
                            "schema_version": 3,
                            "cosine_sims": {"A": 0.42},
                            "literal_cited": {"A": True},
                        }
                    ),
                }
                ok = hub_writer.post_rl_event(event)
            assert ok is True
            rec = _RecordingHandler.requests[0]
            assert rec["body"]["embedding_model"] == "qwen3-embedding:0.6b"
            assert rec["body"]["payload_json"]  # not stripped/normalized
        finally:
            server.shutdown()
            server.server_close()


# ----------------------------------------------------------------------
# 3. Soft-fail paths.
# ----------------------------------------------------------------------


class TestSoftFailPaths:
    def test_no_token_returns_false(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        env.pop("VCT_HUB_PORT", None)
        env["VCT_STATE_DIR"] = str(tmp_path)
        with patch.dict(os.environ, env, clear=True):
            ok = hub_writer.post_rl_event(
                {"event_type": "retrieval", "task_id": "x", "payload_json": "{}"}
            )
            assert ok is False

    def test_connect_refused_returns_false(self, tmp_path: Path) -> None:
        # Point at a random high port nothing is listening on.
        (tmp_path / "hub.token").write_text("tok")
        (tmp_path / "hub.port").write_text("59999")
        env = dict(os.environ)
        env.pop("VCT_HUB_PORT", None)
        env["VCT_STATE_DIR"] = str(tmp_path)
        with patch.dict(os.environ, env, clear=True):
            ok = hub_writer.post_rl_event(
                {"event_type": "retrieval", "task_id": "x", "payload_json": "{}"},
                timeout=0.5,
            )
            assert ok is False

    def test_non_2xx_returns_false(self, tmp_path: Path) -> None:
        server, port = _start_stub_hub()
        try:
            _RecordingHandler.response_status = 400
            _RecordingHandler.response_body = b'{"error":{"code":"bad"}}'
            (tmp_path / "hub.token").write_text("tok")
            (tmp_path / "hub.port").write_text(str(port))
            env = dict(os.environ)
            env.pop("VCT_HUB_PORT", None)
            env["VCT_STATE_DIR"] = str(tmp_path)
            with patch.dict(os.environ, env, clear=True):
                ok = hub_writer.post_rl_event(
                    {"event_type": "retrieval", "task_id": "x", "payload_json": "{}"}
                )
            assert ok is False
        finally:
            server.shutdown()
            server.server_close()

    def test_non_json_serializable_returns_false(self, tmp_path: Path) -> None:
        (tmp_path / "hub.token").write_text("tok")
        env = dict(os.environ)
        env.pop("VCT_HUB_PORT", None)
        env["VCT_STATE_DIR"] = str(tmp_path)
        # Object with a non-serializable value.
        with patch.dict(os.environ, env, clear=True):
            ok = hub_writer.post_rl_event(
                {"event_type": "retrieval", "task_id": "x", "junk": {1, 2, 3}}
            )
            assert ok is False

    def test_5xx_returns_false(self, tmp_path: Path) -> None:
        server, port = _start_stub_hub()
        try:
            _RecordingHandler.response_status = 500
            _RecordingHandler.response_body = b'{"error":{"code":"internal_error"}}'
            (tmp_path / "hub.token").write_text("tok")
            (tmp_path / "hub.port").write_text(str(port))
            env = dict(os.environ)
            env.pop("VCT_HUB_PORT", None)
            env["VCT_STATE_DIR"] = str(tmp_path)
            with patch.dict(os.environ, env, clear=True):
                ok = hub_writer.post_rl_event(
                    {"event_type": "retrieval", "task_id": "x", "payload_json": "{}"}
                )
            assert ok is False
        finally:
            server.shutdown()
            server.server_close()


# ----------------------------------------------------------------------
# 4. Token is read fresh on every call.
# ----------------------------------------------------------------------


class TestTokenIsReadFreshEveryCall:
    """The hub rotates its token on every startup. The Python writer
    survives those restarts, so it MUST re-read the token file on each
    POST rather than caching."""

    def test_token_rewrite_observed_on_next_call(self, tmp_path: Path) -> None:
        server, port = _start_stub_hub()
        try:
            (tmp_path / "hub.token").write_text("tok-A")
            (tmp_path / "hub.port").write_text(str(port))
            env = dict(os.environ)
            env.pop("VCT_HUB_PORT", None)
            env["VCT_STATE_DIR"] = str(tmp_path)
            event = {
                "event_type": "retrieval",
                "task_id": "x",
                "payload_json": "{}",
            }
            with patch.dict(os.environ, env, clear=True):
                hub_writer.post_rl_event(event)
                # Rotate token between calls.
                (tmp_path / "hub.token").write_text("tok-B")
                hub_writer.post_rl_event(event)
            assert len(_RecordingHandler.requests) == 2
            assert _RecordingHandler.requests[0]["auth"] == "Bearer tok-A"
            assert _RecordingHandler.requests[1]["auth"] == "Bearer tok-B"
        finally:
            server.shutdown()
            server.server_close()


# ----------------------------------------------------------------------
# 5. Module exports.
# ----------------------------------------------------------------------


class TestModuleSurface:
    def test_post_rl_event_is_public(self) -> None:
        assert "post_rl_event" in hub_writer.__all__
        assert callable(hub_writer.post_rl_event)
