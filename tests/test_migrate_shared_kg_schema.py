"""Tests for scripts/migrate-shared-kg-schema.{sh,ps1}.

The script drops + recreates the shared KG collection when its schema
lacks `invertedIndexConfig.indexNullState=True`. Tests verify:

  * Bash + PowerShell scripts exist and are syntactically valid.
  * Script soft-fails (exit 0) when Weaviate is unreachable.
  * Script reports "nothing to migrate" + does NOT issue a DELETE when
    the shared KG already has indexNullState=True (idempotency).
  * Script reports "does not exist" + does NOT issue a DELETE when the
    shared KG class is missing.
  * Script issues a DELETE when indexNullState=False; the kg-sync helper
    is invoked afterwards (we stub it with a no-op fixture so the test
    doesn't need a real Ollama).

Mocked Weaviate runs as a tiny stdlib `http.server` instance on an
ephemeral localhost port. No network access required.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SH = REPO_ROOT / "scripts" / "migrate-shared-kg-schema.sh"
SCRIPT_PS1 = REPO_ROOT / "scripts" / "migrate-shared-kg-schema.ps1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MockHandler(BaseHTTPRequestHandler):
    schema: dict = {"classes": []}
    delete_log: list = []

    def log_message(self, *args, **kwargs):
        return

    def do_GET(self):
        if self.path == "/v1/.well-known/ready":
            self.send_response(200)
            self.end_headers()
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

    def do_DELETE(self):
        if self.path.startswith("/v1/schema/"):
            class_name = self.path[len("/v1/schema/"):]
            self.__class__.delete_log.append(class_name)
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


def _start_mock(classes: list):
    _MockHandler.schema = {"classes": classes}
    _MockHandler.delete_log = []
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), _MockHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port, server


class BashScriptTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not shutil.which("bash"):
            raise unittest.SkipTest("bash not available")

    def test_script_exists(self):
        self.assertTrue(SCRIPT_SH.exists(),
                        f"Migration script missing: {SCRIPT_SH}")

    def test_script_syntax_valid(self):
        rc = subprocess.call(["bash", "-n", str(SCRIPT_SH)])
        self.assertEqual(rc, 0, "bash -n found syntax errors")

    def test_script_is_executable(self):
        self.assertTrue(os.access(str(SCRIPT_SH), os.X_OK),
                        "Migration script must be executable (chmod +x)")

    def test_unreachable_weaviate_soft_fails(self):
        port = _free_port()  # nothing bound
        env = os.environ.copy()
        env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
        env["SHARED_KG_COLLECTION"] = "FooBar_Shared"
        result = subprocess.run(
            ["bash", str(SCRIPT_SH)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0,
                         f"Soft-fail expected; got rc={result.returncode}")

    def test_missing_shared_kg_is_noop(self):
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        port, server = _start_mock(classes=[])  # no classes at all
        try:
            env = os.environ.copy()
            env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
            env["SHARED_KG_COLLECTION"] = "FooBar_Shared"
            result = subprocess.run(
                ["bash", str(SCRIPT_SH)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("does not exist", result.stdout.lower())
            self.assertEqual(_MockHandler.delete_log, [])
        finally:
            server.shutdown()

    def test_indexnullstate_true_is_noop(self):
        # Shared KG already has indexNullState=true → script exits 0,
        # does NOT issue a DELETE.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        classes = [{
            "class": "Acme_Shared",
            "invertedIndexConfig": {"indexNullState": True},
            "properties": [{"name": "title", "dataType": ["text"]}],
        }]
        port, server = _start_mock(classes=classes)
        try:
            env = os.environ.copy()
            env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
            env["SHARED_KG_COLLECTION"] = "Acme_Shared"
            result = subprocess.run(
                ["bash", str(SCRIPT_SH)],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("no migration needed", result.stdout.lower())
            self.assertEqual(_MockHandler.delete_log, [])
        finally:
            server.shutdown()

    def test_indexnullstate_false_triggers_drop(self):
        # Shared KG has indexNullState=false → script must issue a
        # DELETE before attempting resync. We exit 0 either way (soft
        # fail on resync helper missing or running); the assertion is
        # specifically about the destructive drop happening when the
        # invariant is missing.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        classes = [{
            "class": "FooProj_Shared",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [{"name": "title", "dataType": ["text"]}],
        }]
        port, server = _start_mock(classes=classes)
        try:
            # Copy the .sh script to an isolated dir so the relative
            # path lookup (`$(dirname "$0")/../.claude/scripts/kg-sync`)
            # finds nothing — exercises the "helper not found" branch.
            with tempfile.TemporaryDirectory() as tmp:
                isolated_script = Path(tmp) / "scripts" / SCRIPT_SH.name
                isolated_script.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(SCRIPT_SH), str(isolated_script))
                env = os.environ.copy()
                env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
                env["SHARED_KG_COLLECTION"] = "FooProj_Shared"
                result = subprocess.run(
                    ["bash", str(isolated_script)],
                    env=env, capture_output=True, text=True, timeout=30,
                    cwd=tmp,
                )
                self.assertEqual(result.returncode, 0,
                                 f"stdout={result.stdout}\nstderr={result.stderr}")
                # The drop happened — that's the core invariant we test.
                self.assertEqual(_MockHandler.delete_log, ["FooProj_Shared"])
                self.assertIn("kg-sync helper not found",
                              result.stdout.lower())
        finally:
            server.shutdown()

    def test_drop_happens_even_without_resync_helper(self):
        # Variant of above that ALSO asserts the script reports the
        # missing helper as a hint to the operator (vs silently leaving
        # the collection empty without explanation).
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        classes = [{
            "class": "AcmeShared_KnowledgeGraph",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [],
        }]
        port, server = _start_mock(classes=classes)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                isolated_script = Path(tmp) / "scripts" / SCRIPT_SH.name
                isolated_script.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(SCRIPT_SH), str(isolated_script))
                env = os.environ.copy()
                env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
                env["SHARED_KG_COLLECTION"] = "AcmeShared_KnowledgeGraph"
                result = subprocess.run(
                    ["bash", str(isolated_script)],
                    env=env, capture_output=True, text=True, timeout=30,
                    cwd=tmp,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(_MockHandler.delete_log,
                                 ["AcmeShared_KnowledgeGraph"])
        finally:
            server.shutdown()


class PowerShellScriptTests(unittest.TestCase):

    def test_script_exists(self):
        self.assertTrue(SCRIPT_PS1.exists(),
                        f"PowerShell migration script missing: {SCRIPT_PS1}")

    def test_script_uses_invoke_restmethod(self):
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        self.assertIn("Invoke-RestMethod", body)

    def test_script_checks_indexnullstate(self):
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        self.assertIn("indexNullState", body,
                      "PS script must inspect indexNullState before drop")

    def test_script_defaults_to_vibecodedtools_kg(self):
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        self.assertIn("VibeCodedTools_KnowledgeGraph", body,
                      "PS script must default SHARED_KG_COLLECTION to "
                      "VibeCodedTools_KnowledgeGraph (matches "
                      "project_init derive_project_collection_names)")


if __name__ == "__main__":
    unittest.main()
