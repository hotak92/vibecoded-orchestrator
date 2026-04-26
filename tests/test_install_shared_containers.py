# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for install.py shared-container detection (Bug 29).

Covers:
    - _probe_http returns the URL on success, None on connection refused / timeout / 4xx
    - _detect_existing_services aggregates the three probes into a dict
    - _ensure_collections POSTs only the missing classes and tolerates "already exists"

The tests start a tiny http.server on a random port to simulate Weaviate /
Ollama responses so we don't need actual containers running.
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# install.py lives at the repo root, sibling to tests/. Importing it pulls
# in argparse / urllib stdlibs only, so import-cost is fine.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    """In-memory mock for Weaviate /v1/schema + arbitrary GETs."""

    # Mutable shared state — set by the test before starting the server.
    schema = {"classes": []}
    posted: list = []
    fail_post = False

    def log_message(self, *_a, **_kw):
        # Silence the default request log so test output stays clean.
        pass

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/v1/schema":
            body = json.dumps(_Handler.schema).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/v1/.well-known/ready":
            self.send_response(200)
            self.end_headers()
        elif self.path == "/api/tags":
            body = b'{"models":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path == "/v1/schema":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            _Handler.posted.append(data)
            if _Handler.fail_post:
                # Simulate Weaviate's "class already exists" response.
                self.send_response(422)
                self.send_header("Content-Type", "application/json")
                err = b'{"error":[{"message":"class already exists"}]}'
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
            else:
                self.send_response(200)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


def _start_server() -> tuple[socketserver.TCPServer, int, threading.Thread]:
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Tiny pause so the socket is accepting before tests run.
    time.sleep(0.05)
    return server, port, thread


class ProbeHttpTests(unittest.TestCase):
    """install._probe_http behavior under common failure modes."""

    def test_probe_http_returns_url_on_200(self):
        server, port, _ = _start_server()
        try:
            url = f"http://127.0.0.1:{port}/v1/.well-known/ready"
            self.assertEqual(install._probe_http(url, timeout=2.0), url)
        finally:
            server.shutdown()

    def test_probe_http_returns_none_on_404(self):
        server, port, _ = _start_server()
        try:
            url = f"http://127.0.0.1:{port}/no-such-path"
            # Our handler responds 404 → 4xx → _probe_http returns None.
            self.assertIsNone(install._probe_http(url, timeout=2.0))
        finally:
            server.shutdown()

    def test_probe_http_returns_none_on_connection_refused(self):
        # Port 1 is privileged / nothing listens there in CI sandboxes.
        url = "http://127.0.0.1:1/"
        self.assertIsNone(install._probe_http(url, timeout=1.0))


class DetectExistingServicesTests(unittest.TestCase):
    def test_returns_dict_with_three_keys(self):
        result = install._detect_existing_services(
            weaviate_port=1, ollama_port=1, code_embed_port=1
        )
        self.assertEqual(
            set(result.keys()),
            {"weaviate_url", "ollama_url", "code_embed_url"},
        )
        # All three target port 1 → all None.
        self.assertEqual(list(result.values()), [None, None, None])

    def test_detects_running_service(self):
        server, port, _ = _start_server()
        try:
            # Same mock server answers all three endpoints, so probe to it
            # for weaviate and leave ollama / code_embed pointing at port 1.
            result = install._detect_existing_services(
                weaviate_port=port, ollama_port=1, code_embed_port=1
            )
            self.assertIsNotNone(result["weaviate_url"])
            self.assertIn(f":{port}", result["weaviate_url"])
            self.assertIsNone(result["ollama_url"])
            self.assertIsNone(result["code_embed_url"])
        finally:
            server.shutdown()


class EnsureCollectionsTests(unittest.TestCase):
    def setUp(self):
        # Reset the shared mock state for every test.
        _Handler.schema = {"classes": []}
        _Handler.posted = []
        _Handler.fail_post = False

    def test_creates_missing_collections(self):
        server, port, _ = _start_server()
        try:
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev"},
            ):
                install._ensure_collections({})
            posted_classes = [p.get("class") for p in _Handler.posted]
            self.assertIn("TestKG", posted_classes)
            self.assertIn("TestDev", posted_classes)
        finally:
            server.shutdown()

    def test_skips_existing_collections(self):
        server, port, _ = _start_server()
        try:
            _Handler.schema = {"classes": [{"class": "TestKG"}, {"class": "TestDev"}]}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev"},
            ):
                install._ensure_collections({})
            # Both collections already in schema → no POSTs.
            self.assertEqual(_Handler.posted, [])
        finally:
            server.shutdown()

    def test_creates_only_missing_subset(self):
        server, port, _ = _start_server()
        try:
            _Handler.schema = {"classes": [{"class": "TestKG"}]}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev"},
            ):
                install._ensure_collections({})
            # Only TestDev should be POSTed; TestKG is already there.
            posted_classes = [p.get("class") for p in _Handler.posted]
            self.assertEqual(posted_classes, ["TestDev"])
        finally:
            server.shutdown()

    def test_already_exists_422_is_benign(self):
        # When a parallel install creates the class first, the POST returns
        # 422 with "already exists" — _ensure_collections must not raise.
        server, port, _ = _start_server()
        try:
            _Handler.fail_post = True
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev"},
            ):
                # No exception expected.
                install._ensure_collections({})
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
