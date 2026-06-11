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
    # v0.2.54 Track D: object inventory served via the GraphQL endpoint
    # for the pre-drop shared-write probe. List of file_path strings.
    objects: list = []
    graphql_fail: bool = False

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

    def do_POST(self):
        # v0.2.54 Track D: GraphQL endpoint for the pre-drop shared-write
        # probe (Aggregate count + Get file_path page).
        if self.path != "/v1/graphql":
            self.send_response(404)
            self.end_headers()
            return
        if self.graphql_fail:
            self.send_response(500)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            query = json.loads(body).get("query", "")
        except json.JSONDecodeError:
            query = ""
        # Class name = first token after "Aggregate {" / "Get {".
        cls_name = None
        for marker in ("Aggregate {", "Get {"):
            if marker in query:
                cls_name = (query.split(marker, 1)[1]
                            .split("(", 1)[0].split("{", 1)[0].strip())
                break
        payload: dict = {"data": {}}
        if "Aggregate {" in query:
            payload["data"]["Aggregate"] = {
                cls_name: [{"meta": {"count": len(self.objects)}}],
            }
        elif "Get {" in query:
            payload["data"]["Get"] = {
                cls_name: [{"file_path": fp} for fp in self.objects],
            }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(encoded)


def _start_mock(classes: list, objects: list = None, graphql_fail: bool = False):
    _MockHandler.schema = {"classes": classes}
    _MockHandler.delete_log = []
    _MockHandler.objects = list(objects or [])
    _MockHandler.graphql_fail = graphql_fail
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

    # ------------------------------------------------------------------
    # v0.2.54 Track D (P0-2): pre-drop guard tests. The pre-fix contract
    # ("drop happens even without resync helper", exit 0) was the
    # data-loss bug itself — the assertions below pin the INVERTED
    # behaviour: no kg-sync → exit 4, NO drop; unrecoverable
    # cross-project nodes → exit 3, NO drop unless consented.
    # ------------------------------------------------------------------

    @staticmethod
    def _isolated_clone(tmp: Path, *, with_kg_sync: bool,
                        knowledge_files: list = ()) -> Path:
        """Build a minimal fake orchestrator clone with the migration
        script at scripts/, optionally an executable kg-sync stub that
        records its invocation, and optional knowledge/ source files."""
        script = tmp / "scripts" / SCRIPT_SH.name
        script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(SCRIPT_SH), str(script))
        if with_kg_sync:
            kg_sync = tmp / ".claude" / "scripts" / "kg-sync"
            kg_sync.parent.mkdir(parents=True, exist_ok=True)
            kg_sync.write_text(
                "#!/usr/bin/env bash\n"
                f"echo \"$@\" > '{tmp}/kg-sync-invoked.txt'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            kg_sync.chmod(0o755)
        for rel in knowledge_files:
            f = tmp / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("# stub node\n", encoding="utf-8")
        return script

    def test_missing_kg_sync_aborts_before_drop(self):
        # GUARD 1: no resync helper → exit 4 and NO DELETE issued.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        classes = [{
            "class": "FooProj_Shared",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [{"name": "title", "dataType": ["text"]}],
        }]
        port, server = _start_mock(classes=classes)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                script = self._isolated_clone(Path(tmp), with_kg_sync=False)
                env = os.environ.copy()
                env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
                env["SHARED_KG_COLLECTION"] = "FooProj_Shared"
                env.pop("VCO_SHARED_KG_MIGRATE_CONSENT", None)
                result = subprocess.run(
                    ["bash", str(script)],
                    env=env, capture_output=True, text=True, timeout=30,
                    cwd=tmp,
                )
                self.assertEqual(result.returncode, 4,
                                 f"stdout={result.stdout}\nstderr={result.stderr}")
                self.assertEqual(_MockHandler.delete_log, [],
                                 "collection must NOT be dropped when "
                                 "kg-sync is missing")
                self.assertIn("kg-sync helper not found", result.stderr.lower())
        finally:
            server.shutdown()

    def test_cross_project_nodes_refused_without_consent(self):
        # GUARD 2: a stored file_path that does NOT resolve under the
        # clone root (= written by another project) → exit 3, NO drop.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        classes = [{
            "class": "FooProj_Shared",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [],
        }]
        port, server = _start_mock(
            classes=classes,
            objects=["knowledge/concepts/local-node.md",
                     "knowledge/concepts/from-other-project.md"],
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                script = self._isolated_clone(
                    Path(tmp), with_kg_sync=True,
                    # Only ONE of the two stored paths exists locally.
                    knowledge_files=["knowledge/concepts/local-node.md"],
                )
                env = os.environ.copy()
                env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
                env["SHARED_KG_COLLECTION"] = "FooProj_Shared"
                env.pop("VCO_SHARED_KG_MIGRATE_CONSENT", None)
                result = subprocess.run(
                    ["bash", str(script)],
                    env=env, capture_output=True, text=True, timeout=30,
                    cwd=tmp,
                )
                self.assertEqual(result.returncode, 3,
                                 f"stdout={result.stdout}\nstderr={result.stderr}")
                self.assertEqual(_MockHandler.delete_log, [])
                normalized = " ".join(result.stderr.lower().split())
                self.assertIn("permanently lost", normalized)
                self.assertIn("from-other-project.md", result.stderr)
                self.assertIn("VCO_SHARED_KG_MIGRATE_CONSENT=1", result.stderr)
        finally:
            server.shutdown()

    def test_consent_env_allows_drop_and_resync(self):
        # Same unrecoverable-node scenario + explicit consent → drop
        # proceeds and the resync helper runs.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        classes = [{
            "class": "FooProj_Shared",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [],
        }]
        port, server = _start_mock(
            classes=classes,
            objects=["knowledge/concepts/from-other-project.md"],
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                script = self._isolated_clone(Path(tmp), with_kg_sync=True)
                env = os.environ.copy()
                env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
                env["SHARED_KG_COLLECTION"] = "FooProj_Shared"
                env["VCO_SHARED_KG_MIGRATE_CONSENT"] = "1"
                result = subprocess.run(
                    ["bash", str(script)],
                    env=env, capture_output=True, text=True, timeout=30,
                    cwd=tmp,
                )
                self.assertEqual(result.returncode, 0,
                                 f"stdout={result.stdout}\nstderr={result.stderr}")
                self.assertEqual(_MockHandler.delete_log, ["FooProj_Shared"])
                self.assertTrue((Path(tmp) / "kg-sync-invoked.txt").is_file(),
                                "resync helper must run after consented drop")
        finally:
            server.shutdown()

    def test_all_paths_restorable_proceeds_without_consent(self):
        # Every stored file_path resolves under the clone root → the
        # original premise holds → migration proceeds with no consent.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        classes = [{
            "class": "FooProj_Shared",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [],
        }]
        port, server = _start_mock(
            classes=classes,
            objects=["knowledge/concepts/a.md", "knowledge/tools/b.md"],
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                script = self._isolated_clone(
                    Path(tmp), with_kg_sync=True,
                    knowledge_files=["knowledge/concepts/a.md",
                                     "knowledge/tools/b.md"],
                )
                env = os.environ.copy()
                env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
                env["SHARED_KG_COLLECTION"] = "FooProj_Shared"
                env.pop("VCO_SHARED_KG_MIGRATE_CONSENT", None)
                result = subprocess.run(
                    ["bash", str(script)],
                    env=env, capture_output=True, text=True, timeout=30,
                    cwd=tmp,
                )
                self.assertEqual(result.returncode, 0,
                                 f"stdout={result.stdout}\nstderr={result.stderr}")
                self.assertEqual(_MockHandler.delete_log, ["FooProj_Shared"])
                self.assertTrue((Path(tmp) / "kg-sync-invoked.txt").is_file())
        finally:
            server.shutdown()

    def test_probe_failure_refused_without_consent(self):
        # GraphQL probe failing (HTTP 500) → cannot verify → exit 3,
        # NO drop. Conservative-by-design.
        if not shutil.which("jq"):
            self.skipTest("jq not available")
        classes = [{
            "class": "FooProj_Shared",
            "invertedIndexConfig": {"indexNullState": False},
            "properties": [],
        }]
        port, server = _start_mock(classes=classes, graphql_fail=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                script = self._isolated_clone(Path(tmp), with_kg_sync=True)
                env = os.environ.copy()
                env["WEAVIATE_URL"] = f"http://127.0.0.1:{port}"
                env["SHARED_KG_COLLECTION"] = "FooProj_Shared"
                env.pop("VCO_SHARED_KG_MIGRATE_CONSENT", None)
                result = subprocess.run(
                    ["bash", str(script)],
                    env=env, capture_output=True, text=True, timeout=30,
                    cwd=tmp,
                )
                self.assertEqual(result.returncode, 3,
                                 f"stdout={result.stdout}\nstderr={result.stderr}")
                self.assertEqual(_MockHandler.delete_log, [])
                self.assertIn("could not verify", result.stderr.lower())
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

    def test_script_defaults_to_canonical_shared_kg(self):
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        # PR-34 (v0.2.12, Group M): canonical name unified across all
        # surfaces (renamed from VibeCodedTools_KnowledgeGraph in PR-26).
        # v0.2.23 B1 (2026-05-21) flipped the canonical casing from
        # lowercase-c back to capital-C "VibeCoded" to match the brand
        # spelling. The literal must match
        # vco_lib/project_init.py::_SHARED_KG_NAME and the cross-language
        # invariant in tests/test_shared_kg_constant_consistency.py.
        self.assertIn("VibeCodedOrchestrator_KnowledgeGraph", body,
                      "PS script must default SHARED_KG_COLLECTION to "
                      "VibeCodedOrchestrator_KnowledgeGraph (matches "
                      "project_init derive_project_collection_names)")

    # v0.2.54 Track D: parity assertions for the pre-drop guards.

    def test_ps1_has_consent_env_gate(self):
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        self.assertIn("VCO_SHARED_KG_MIGRATE_CONSENT", body,
                      "PS script must honor the same consent env var as the .sh")

    def test_ps1_has_refusal_exit_codes(self):
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        self.assertIn("exit 3", body, "refusal (unrecoverable nodes) exit code")
        self.assertIn("exit 4", body, "refusal (kg-sync missing) exit code")

    def test_ps1_guards_run_before_drop(self):
        body = SCRIPT_PS1.read_text(encoding="utf-8")
        drop_idx = body.index("-Method Delete")
        kg_sync_guard_idx = body.index("GUARD 1")
        probe_guard_idx = body.index("GUARD 2")
        self.assertLess(kg_sync_guard_idx, drop_idx,
                        "kg-sync presence check must precede the DELETE")
        self.assertLess(probe_guard_idx, drop_idx,
                        "shared-write probe must precede the DELETE")

    def test_sh_guards_run_before_drop(self):
        body = SCRIPT_SH.read_text(encoding="utf-8")
        drop_idx = body.index("-X DELETE")
        self.assertLess(body.index("GUARD 1"), drop_idx)
        self.assertLess(body.index("GUARD 2"), drop_idx)


if __name__ == "__main__":
    unittest.main()
