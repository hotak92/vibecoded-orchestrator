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
import os
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
            # Pre-create the shared collection so this test stays focused on
            # KG + Dev (covered separately in test_creates_shared_kg_collection).
            _Handler.schema = {"classes": [{"class": "TestShared"}]}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev",
                 "SHARED_KG_COLLECTION": "TestShared"},
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
            # Pre-seed all three collections (project + dev + shared) so
            # _ensure_collections has nothing to POST. The shared collection
            # name comes from SHARED_KG_COLLECTION (default
            # VibecodedOrchestrator_KnowledgeGraph) — pin it explicitly here to
            # decouple the test from the default value.
            _Handler.schema = {"classes": [
                {"class": "TestKG"},
                {"class": "TestDev"},
                {"class": "TestShared"},
            ]}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev",
                 "SHARED_KG_COLLECTION": "TestShared"},
            ):
                install._ensure_collections({})
            # All three collections already in schema → no POSTs.
            self.assertEqual(_Handler.posted, [])
        finally:
            server.shutdown()

    def test_creates_only_missing_subset(self):
        server, port, _ = _start_server()
        try:
            # Pre-seed project KG and shared KG; only the dev collection
            # should be POSTed.
            _Handler.schema = {"classes": [
                {"class": "TestKG"},
                {"class": "TestShared"},
            ]}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev",
                 "SHARED_KG_COLLECTION": "TestShared"},
            ):
                install._ensure_collections({})
            # Only TestDev should be POSTed; TestKG + TestShared already there.
            posted_classes = [p.get("class") for p in _Handler.posted]
            self.assertEqual(posted_classes, ["TestDev"])
        finally:
            server.shutdown()

    def test_creates_shared_kg_collection(self):
        """Step 7b also bootstraps the cross-project shared KG collection so
        every install on this machine sees the same VibecodedOrchestrator_KnowledgeGraph."""
        server, port, _ = _start_server()
        try:
            _Handler.schema = {"classes": []}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev",
                 "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph"},
            ):
                install._ensure_collections({})
            posted_classes = [p.get("class") for p in _Handler.posted]
            self.assertIn("TestKG", posted_classes)
            self.assertIn("TestDev", posted_classes)
            self.assertIn("VibecodedOrchestrator_KnowledgeGraph", posted_classes)
            # Shared collection schema mirrors the project KG (same builder).
            shared = next(p for p in _Handler.posted
                          if p.get("class") == "VibecodedOrchestrator_KnowledgeGraph")
            prop_names = {p["name"] for p in shared["properties"]}
            self.assertIn("title", prop_names)
            self.assertIn("content", prop_names)
            self.assertIn("typed_links", prop_names)
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


def _ns(**overrides):
    """Minimal argparse-like namespace for _ensure_collections."""
    ns = type("Ns", (), {})()
    ns.yes = overrides.get("yes", True)
    ns.quiet = overrides.get("quiet", False)
    ns.skip_seed = overrides.get("skip_seed", False)
    ns.skip_collections = overrides.get("skip_collections", False)
    return ns


class EnsureCollectionsAdoptModeTests(unittest.TestCase):
    """Adopt-mode safety: per-install naming, skip-if-exists, --skip-seed."""

    def setUp(self):
        _Handler.schema = {"classes": []}
        _Handler.posted = []
        _Handler.fail_post = False

    def test_adopt_does_not_create_bare_kg_when_host_namespaced(self):
        """Installing into a Weaviate that uses per-project namespacing
        (e.g. Acme_KnowledgeGraph, ClaudeKnowledgeGraph) must NOT create
        the bare top-level `KnowledgeGraph` / `Development` classes."""
        server, port, _ = _start_server()
        try:
            # Simulate the user's existing Weaviate from the bug report.
            _Handler.schema = {"classes": [
                {"class": "Acme_KnowledgeGraph",
                 "properties": [{"name": "typed_links"}]},
                {"class": "Acme_CodeFunction"},
                {"class": "ClaudeKnowledgeGraph",
                 "properties": [{"name": "typed_links"}]},
                {"class": "ImageDataset_KnowledgeGraph",
                 "properties": [{"name": "typed_links"}]},
                {"class": "ClaudeOrchestrator_development"},
            ]}
            decisions = {"weaviate": {"action": install.ACTION_ADOPT}}
            # Clear any inherited env so the basename derivation runs.
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port)},
                clear=False,
            ):
                for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                          "SHARED_KG_COLLECTION"):
                    os.environ.pop(k, None) if k in os.environ else None  # type: ignore
                install._ensure_collections(
                    {}, decisions=decisions, args=_ns(yes=True),
                )
            posted_classes = [p.get("class") for p in _Handler.posted]
            # The exact failure mode from the bug brief:
            self.assertNotIn("KnowledgeGraph", posted_classes,
                             "must not create bare KnowledgeGraph in adopt mode")
            self.assertNotIn("Development", posted_classes,
                             "must not create bare Development in adopt mode")
            # Per-install name is derived from PROJECT_ROOT basename. Basename
            # is whatever directory install.py lives in — just assert it's a
            # sane *_KnowledgeGraph or *_Development.
            #
            # Edge case (post-PR-26/34): when PROJECT_ROOT.basename is
            # "vibecoded-orchestrator" → sanitized → "VibecodedOrchestrator",
            # the project-scoped KG name HAPPENS to equal the canonical
            # shared KG name (VibecodedOrchestrator_KnowledgeGraph). That's
            # legitimate — the orchestrator-self IS a managed project
            # whose own KG aliases the shared name. Accept this case by
            # checking that SOMETHING project-scoped (KG OR Development)
            # got created, not strictly excluding the shared name.
            project_scoped = [c for c in posted_classes
                              if (c.endswith("_KnowledgeGraph")
                                  or c.endswith("_Development"))
                              and c not in ("ClaudeKnowledgeGraph",
                                            "Acme_KnowledgeGraph",
                                            "ImageDataset_KnowledgeGraph",
                                            "ClaudeOrchestrator_development")]
            self.assertTrue(
                project_scoped,
                f"expected a project-scoped *_KnowledgeGraph or *_Development; got {posted_classes}",
            )
        finally:
            # Clean up env mutations done by _ensure_collections.
            for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                      "SHARED_KG_COLLECTION"):
                os.environ.pop(k, None)
            server.shutdown()

    def test_adopt_creates_own_dev_even_when_sibling_dev_exists(self):
        # Per-project `_development` collections are NOT shared — each
        # project owns its own namespace. Earlier code skipped ours when
        # a sibling's existed (e.g. ClaudeOrchestrator_development),
        # which left our docs unseeded (Step 7c then exited 1). Fixed
        # in 2026-04-27 — assert the project-scoped collection is
        # always created in adopt mode.
        #
        # The expected dev-collection name depends on `install.PROJECT_ROOT`
        # (PascalCased basename + `_Development`). When the test is run
        # from a worktree (e.g. /tmp/vco-kg-port) the name differs from
        # the production "VibecodedOrchestrator". Derive the expected
        # name dynamically so the test is worktree-agnostic — both the
        # main checkout and any /tmp/<x> worktree pass.
        expected_dev = install._derive_project_dev_name(install.PROJECT_ROOT)

        server, port, _ = _start_server()
        try:
            _Handler.schema = {"classes": [
                {"class": "ClaudeOrchestrator_development"},
            ]}
            decisions = {"weaviate": {"action": install.ACTION_ADOPT}}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port)},
                clear=False,
            ):
                for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                          "SHARED_KG_COLLECTION"):
                    os.environ.pop(k, None)
                install._ensure_collections(
                    {}, decisions=decisions, args=_ns(yes=True),
                )
            posted_classes = [p.get("class") for p in _Handler.posted]
            # Our own per-project `_development` must be created.
            self.assertIn(
                expected_dev, posted_classes,
                f"expected {expected_dev} to be created; "
                f"posted: {posted_classes}",
            )
            # We must NOT touch the sibling project's collection.
            self.assertNotIn(
                "ClaudeOrchestrator_development", posted_classes,
                "must not re-post a sibling project's existing collection",
            )
        finally:
            for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                      "SHARED_KG_COLLECTION"):
                os.environ.pop(k, None)
            server.shutdown()

    def test_skip_seed_skips_collection_creation(self):
        """--skip-seed must short-circuit the whole step (no schema POSTs)."""
        server, port, _ = _start_server()
        try:
            _Handler.schema = {"classes": []}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev",
                 "SHARED_KG_COLLECTION": "TestShared"},
            ):
                install._ensure_collections(
                    {}, decisions={}, args=_ns(yes=True, skip_seed=True),
                )
            self.assertEqual(_Handler.posted, [],
                             "--skip-seed must not POST any schema")
        finally:
            server.shutdown()

    def test_skip_collections_flag(self):
        server, port, _ = _start_server()
        try:
            _Handler.schema = {"classes": []}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port), "KG_COLLECTION": "TestKG",
                 "DEVELOPMENT_COLLECTION": "TestDev",
                 "SHARED_KG_COLLECTION": "TestShared"},
            ):
                install._ensure_collections(
                    {}, decisions={}, args=_ns(yes=True, skip_collections=True),
                )
            self.assertEqual(_Handler.posted, [])
        finally:
            server.shutdown()

    def test_self_managed_uses_derived_kg_name(self):
        """When we own the Weaviate (not adopt), derive per-project names from
        PROJECT_ROOT basename — bare `KnowledgeGraph` / `Development` collide
        with sibling installs sharing the same Weaviate. Was previously
        ``test_self_managed_keeps_bare_defaults``; the bare-default behaviour
        was the source of the VideoFrames KG-collision bug fixed 2026-05-01.
        """
        server, port, _ = _start_server()
        try:
            _Handler.schema = {"classes": []}
            decisions = {"weaviate": {"action": install.ACTION_START}}
            with mock.patch.dict(
                "os.environ",
                {"WEAVIATE_PORT": str(port)},
                clear=False,
            ):
                for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                          "SHARED_KG_COLLECTION"):
                    os.environ.pop(k, None)
                install._ensure_collections(
                    {}, decisions=decisions, args=_ns(yes=True),
                )
            posted_classes = [p.get("class") for p in _Handler.posted]
            expected_kg = install._derive_project_kg_name(install.PROJECT_ROOT)
            expected_dev = install._derive_project_dev_name(install.PROJECT_ROOT)
            self.assertIn(expected_kg, posted_classes)
            self.assertIn(expected_dev, posted_classes)
            # Defence in depth: bare names must NOT be created.
            self.assertNotIn("KnowledgeGraph", posted_classes)
            self.assertNotIn("Development", posted_classes)
        finally:
            for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                      "SHARED_KG_COLLECTION"):
                os.environ.pop(k, None)
            server.shutdown()


class DeriveProjectKgNameTests(unittest.TestCase):
    """install._derive_project_kg_name basename → class-name conversion."""

    def test_simple_basename(self):
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/myapp")),
            "Myapp_KnowledgeGraph",
        )

    def test_basename_with_hyphens(self):
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/vibecoded-orchestrator")),
            "VibecodedOrchestrator_KnowledgeGraph",
        )

    def test_basename_with_underscores(self):
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/test_install")),
            "TestInstall_KnowledgeGraph",
        )

    def test_basename_with_special_chars_only(self):
        # Edge case: path basename is all punctuation. Falls back to the
        # `vct_` prefix to keep the install valid.
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/...")),
            "vct_KnowledgeGraph",
        )

    def test_basename_starting_with_digit(self):
        # Leading digit on its own would yield "1Foo" — invalid Weaviate
        # class name (must start with letter). Fallback prefix.
        self.assertEqual(
            install._derive_project_kg_name(Path("/x/y/1foo")),
            "vct_KnowledgeGraph",
        )


class ProbeServiceIdentityTests(unittest.TestCase):
    """install._probe_service_identity content-fingerprinting."""

    def setUp(self):
        _Handler.schema = {"classes": []}
        _Handler.posted = []
        _Handler.fail_post = False

    def test_not_running_when_port_unreachable(self):
        # Port 1 — no listener.
        result, _evidence = install._probe_service_identity("weaviate", port=1)
        self.assertEqual(result, install.PROBE_NOT_RUNNING)

    def test_weaviate_with_vct_collections_is_managed(self):
        # Pre-seed schema with our marker.
        _Handler.schema = {"classes": [{"class": "KnowledgeGraph"}]}
        server, port, _ = _start_server()
        try:
            with mock.patch.object(install, "_read_services_toml",
                                   return_value={"services": []}):
                result, evidence = install._probe_service_identity(
                    "weaviate", port=port,
                )
            self.assertEqual(result, install.PROBE_VCT_MANAGED)
            self.assertIn("KnowledgeGraph", evidence)
        finally:
            server.shutdown()

    def test_weaviate_with_no_vct_collections_is_foreign(self):
        # Empty schema, no services.toml lock — a fresh foreign Weaviate.
        _Handler.schema = {"classes": []}
        server, port, _ = _start_server()
        try:
            with mock.patch.object(install, "_read_services_toml",
                                   return_value={"services": []}):
                result, _evidence = install._probe_service_identity(
                    "weaviate", port=port,
                )
            self.assertEqual(result, install.PROBE_FOREIGN)
        finally:
            server.shutdown()

    def test_services_toml_lock_overrides_content_probe(self):
        # Schema is empty (would otherwise be foreign) — but services.toml
        # has a prior `adopt` decision → vct_managed.
        _Handler.schema = {"classes": []}
        server, port, _ = _start_server()
        try:
            with mock.patch.object(install, "_read_services_toml", return_value={
                "services": [{"name": "weaviate", "mode": "adopt"}],
            }):
                result, evidence = install._probe_service_identity(
                    "weaviate", port=port,
                )
            self.assertEqual(result, install.PROBE_VCT_MANAGED)
            self.assertIn("prior decision", evidence)
        finally:
            server.shutdown()


class DecideActionTests(unittest.TestCase):
    """install._decide_action — non-interactive resolution paths only."""

    def _args(self, **overrides):
        ns = type("Ns", (), {})()
        ns.on_conflict = overrides.get("on_conflict", None)
        ns.yes = overrides.get("yes", True)  # default to non-interactive
        ns.quiet = overrides.get("quiet", False)
        return ns

    def test_not_running_starts(self):
        action = install._decide_action(
            "weaviate", install.PROBE_NOT_RUNNING, "n/a", self._args(),
        )
        self.assertEqual(action, install.ACTION_START)

    def test_managed_adopts(self):
        action = install._decide_action(
            "weaviate", install.PROBE_VCT_MANAGED, "n/a", self._args(),
        )
        self.assertEqual(action, install.ACTION_ADOPT)

    def test_incompatible_aborts(self):
        action = install._decide_action(
            "weaviate", install.PROBE_INCOMPATIBLE, "n/a", self._args(),
        )
        self.assertEqual(action, install.ACTION_ABORT)

    def test_foreign_default_is_alt_port(self):
        # No --on-conflict; --yes (non-interactive) → alt-port (the safe default).
        action = install._decide_action(
            "weaviate", install.PROBE_FOREIGN, "n/a", self._args(),
        )
        self.assertEqual(action, install.ACTION_ALT_PORT)

    def test_foreign_with_on_conflict_adopt(self):
        action = install._decide_action(
            "weaviate", install.PROBE_FOREIGN, "n/a",
            self._args(on_conflict="adopt"),
        )
        self.assertEqual(action, install.ACTION_ADOPT)

    def test_foreign_with_on_conflict_abort(self):
        action = install._decide_action(
            "weaviate", install.PROBE_FOREIGN, "n/a",
            self._args(on_conflict="abort"),
        )
        self.assertEqual(action, install.ACTION_ABORT)


class FindFreePortTests(unittest.TestCase):
    def test_returns_free_port_above_default(self):
        # Bind to a known port; ask for the next free one.
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            taken = s.getsockname()[1]
            free = install._find_free_port(taken + 1)
            self.assertIsNotNone(free)
            self.assertGreater(free, taken)


class ServicesTomlRoundtripTests(unittest.TestCase):
    """install._write_services_toml + _read_services_toml end-to-end.

    Schema must be readable by both Python (tomllib) and the launcher's
    Rust toml crate (services::adoption::AdoptionState).
    """

    def test_roundtrip_via_tomllib(self):
        import tempfile
        import tomllib
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "services.toml"
            with mock.patch.object(install, "_services_toml_path",
                                   return_value=path):
                state = {"services": [
                    {"name": "weaviate", "mode": "parallel",
                     "external_url": "http://localhost:8081",
                     "parallel_port": 8082},
                    {"name": "ollama", "mode": "unresolved",
                     "external_url": None, "parallel_port": None},
                ]}
                install._write_services_toml(state)
                roundtripped = tomllib.loads(path.read_text())
            services = roundtripped["services"]
            self.assertEqual(len(services), 2)
            self.assertEqual(services[0]["name"], "weaviate")
            self.assertEqual(services[0]["parallel_port"], 8082)
            self.assertEqual(services[1]["mode"], "unresolved")
            # Optional fields omitted when None — matches launcher's
            # serde(skip_serializing_if = "Option::is_none") behavior on
            # AdoptionState.
            self.assertNotIn("parallel_port", services[1])
            self.assertNotIn("external_url", services[1])


if __name__ == "__main__":
    unittest.main()
