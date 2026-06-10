"""Tests for scripts/migrate-development-temporal-props.{sh,ps1}.

The script adds the four canonical temporal properties (`created`,
`updated`, `valid_from`, `valid_until`) to every existing
`*_Development` collection in a running Weaviate. This test suite
verifies:

  * Bash + PowerShell scripts exist and are syntactically valid.
  * The bash script soft-fails (exit 0) when Weaviate is unreachable.
  * The bash script soft-fails (exit 0) when no `*_Development`
    collections exist.
  * Against a mocked Weaviate REST endpoint, the script POSTs the
    correct property payloads for missing props and SKIPS props that
    are already present (idempotency).

Mocked Weaviate runs as a tiny stdlib `http.server.BaseHTTPRequestHandler`
on an ephemeral localhost port. No network access required.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SH = REPO_ROOT / "scripts" / "migrate-development-temporal-props.sh"
SCRIPT_PS1 = REPO_ROOT / "scripts" / "migrate-development-temporal-props.ps1"


def _free_port() -> int:
    """Bind to port 0 to claim a free port, then release."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MockWeaviateHandler(BaseHTTPRequestHandler):
    """In-memory mock of the v1 schema REST API.

    Class-level state (`schema`, `post_log`) is mutated across requests
    so the test can inspect what the script sent.
    """

    schema: dict = {"classes": []}
    post_log: list = []

    def log_message(self, *args, **kwargs):  # silence handler noise
        return

    def do_GET(self):
        if self.path == "/v1/.well-known/ready":
            self.send_response(200)
            self.end_headers()
            return
        if self.path == "/v1/schema":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.schema).encode())
            return
        if self.path.startswith("/v1/schema/"):
            class_name = self.path[len("/v1/schema/"):]
            for cls in self.schema["classes"]:
                if cls["class"] == class_name:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(cls).encode())
                    return
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        # POST /v1/schema/<class>/properties — add a new property.
        if self.path.startswith("/v1/schema/") and self.path.endswith("/properties"):
            class_name = self.path[len("/v1/schema/"):-len("/properties")]
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode()
            prop = json.loads(body)
            self.__class__.post_log.append({"class": class_name, "prop": prop})
            for cls in self.schema["classes"]:
                if cls["class"] == class_name:
                    existing = {p["name"] for p in cls.get("properties", [])}
                    if prop["name"] in existing:
                        self.send_response(422)
                        self.end_headers()
                        return
                    cls.setdefault("properties", []).append(prop)
                    self.send_response(200)
                    self.end_headers()
                    return
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def _start_mock_weaviate(initial_classes: list):
    """Spin up the mock Weaviate on an ephemeral port, return (port, server)."""
    _MockWeaviateHandler.schema = {"classes": initial_classes}
    _MockWeaviateHandler.post_log = []
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _MockWeaviateHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return port, server


class BashScriptTests(unittest.TestCase):
    """Bash script: existence, syntax, soft-fail, idempotency."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("bash"):
            raise unittest.SkipTest("bash not available")

    def test_script_exists(self):
        self.assertTrue(SCRIPT_SH.exists(),
                        f"Migration script missing: {SCRIPT_SH}")

    def test_script_syntax_valid(self):
        # bash -n is a parse-only check; returns 0 if syntactically OK.
        rc = subprocess.call(["bash", "-n", str(SCRIPT_SH)])
        self.assertEqual(rc, 0, "bash -n found syntax errors")

    def test_script_is_executable(self):
        self.assertTrue(os.access(str(SCRIPT_SH), os.X_OK),
                        "Migration script must be executable (chmod +x)")

    def test_unreachable_weaviate_soft_fails(self):
        # Point at an unreachable URL — script must exit 0 (soft-fail).
        port = _free_port()  # nothing bound to it
        env = os.environ.copy()
        env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
        result = subprocess.run(
            ["bash", str(SCRIPT_SH)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0,
                         f"Soft-fail expected; got rc={result.returncode}\n"
                         f"stderr={result.stderr}")

    def test_no_dev_collections_is_noop(self):
        # Empty schema → script reports "nothing to migrate", exits 0.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        port, server = _start_mock_weaviate(initial_classes=[])
        try:
            env = os.environ.copy()
            env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
            result = subprocess.run(
                ["bash", str(SCRIPT_SH)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("nothing to migrate", result.stdout.lower() +
                          result.stderr.lower())
            self.assertEqual(_MockWeaviateHandler.post_log, [])
        finally:
            server.shutdown()

    def test_adds_missing_temporal_props(self):
        # Dev collection has title only → script adds 4 temporal props.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        initial = [{
            "class": "Foo_Development",
            "properties": [{"name": "title", "dataType": ["text"]}],
        }]
        port, server = _start_mock_weaviate(initial_classes=initial)
        try:
            env = os.environ.copy()
            env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
            result = subprocess.run(
                ["bash", str(SCRIPT_SH)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0,
                             f"stdout={result.stdout}\nstderr={result.stderr}")
            posted = {entry["prop"]["name"] for entry in _MockWeaviateHandler.post_log}
            self.assertEqual(posted,
                             {"created", "updated", "valid_from", "valid_until"})
            # All POSTs must be against the dev collection with date dataType.
            for entry in _MockWeaviateHandler.post_log:
                self.assertEqual(entry["class"], "Foo_Development")
                self.assertEqual(entry["prop"]["dataType"], ["date"])
        finally:
            server.shutdown()

    def test_idempotent_when_props_present(self):
        # All 4 temporal props already present → script skips all, exits 0,
        # POSTs zero requests.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        initial = [{
            "class": "Bar_Development",
            "properties": [
                {"name": "title", "dataType": ["text"]},
                {"name": "created", "dataType": ["date"]},
                {"name": "updated", "dataType": ["date"]},
                {"name": "valid_from", "dataType": ["date"]},
                {"name": "valid_until", "dataType": ["date"]},
            ],
        }]
        port, server = _start_mock_weaviate(initial_classes=initial)
        try:
            env = os.environ.copy()
            env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
            result = subprocess.run(
                ["bash", str(SCRIPT_SH)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(_MockWeaviateHandler.post_log, [],
                             "Idempotent re-run must NOT POST any properties")
        finally:
            server.shutdown()

    def test_only_targets_orchestrator_managed_collections(self):
        # V52-I Fix B (2026-06-09) broadened the script to target the
        # full orchestrator-managed quartet of class suffixes:
        # _KnowledgeGraph, _Development, _Diagrams. Other (third-party)
        # classes must not be touched.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        initial = [
            {"class": "Acme_KnowledgeGraph",
             "properties": [{"name": "title", "dataType": ["text"]}]},
            {"class": "Acme_Development",
             "properties": [{"name": "title", "dataType": ["text"]}]},
            {"class": "Acme_Diagrams",
             "properties": [{"name": "title", "dataType": ["text"]}]},
            {"class": "RandomOther",
             "properties": [{"name": "title", "dataType": ["text"]}]},
        ]
        port, server = _start_mock_weaviate(initial_classes=initial)
        try:
            env = os.environ.copy()
            env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
            result = subprocess.run(
                ["bash", str(SCRIPT_SH)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0)
            targeted_classes = {e["class"] for e in _MockWeaviateHandler.post_log}
            # Must touch all 3 orchestrator-managed; must NOT touch RandomOther.
            self.assertEqual(
                targeted_classes,
                {"Acme_KnowledgeGraph", "Acme_Development", "Acme_Diagrams"},
            )
        finally:
            server.shutdown()


class PowerShellScriptTests(unittest.TestCase):
    """PowerShell script: existence + light static checks (no pwsh required)."""

    def test_script_exists(self):
        self.assertTrue(SCRIPT_PS1.exists(),
                        f"PowerShell migration script missing: {SCRIPT_PS1}")

    def test_script_mentions_required_properties(self):
        # Visual-syntax check: the canonical property names must be in the
        # script body. Catches drift between the .sh and .ps1 variants.
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        for prop in ("created", "updated", "valid_from", "valid_until"):
            self.assertIn(prop, body,
                          f"PowerShell script missing reference to '{prop}'")
        self.assertIn("_Development", body,
                      "PowerShell script must filter on _Development suffix")

    def test_script_uses_invoke_restmethod(self):
        # Light cross-check that the PS script uses the right cmdlet
        # (no curl dependency on Windows).
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        self.assertIn("Invoke-RestMethod", body)


if __name__ == "__main__":
    unittest.main()
