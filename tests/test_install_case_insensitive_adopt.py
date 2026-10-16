# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.23 B1 (2026-05-21) — case-insensitive collection adoption, narrowed
in v0.2.103 (owner ruling 2026-10-15).

The behaviour under test lives in ``install.py::_ensure_collections``, run
here as a NON-root install (the root's names are ``vco_lib.root_kg_identity``'s
decision — ``tests/test_v02103_root_kg_identity.py``). When a Weaviate class
exists whose name differs from a required name ONLY by casing:

  * the per-project KG and the development collection adopt it in place and
    propagate the on-disk casing to ``KG_COLLECTION`` /
    ``DEVELOPMENT_COLLECTION`` (legacy behaviour, kept — an existing install
    may depend on it);
  * the SHARED name is never case-adopted: ``SHARED_KG_COLLECTION`` keeps the
    requested name and nothing is created beside the sibling (Weaviate would
    refuse it). Adopting it is an explicit consent step.

These tests stub out Weaviate via an HTTP server (see
``test_install_shared_containers.py`` for the same pattern — we copy the
fixture here rather than refactor a shared helper, since
``test_install_shared_containers.py`` carries other adopt-mode
infrastructure not relevant to the case-insensitive sweep).
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402


# ─── Stub Weaviate HTTP server ────────────────────────────────────────────


class _Handler(http.server.BaseHTTPRequestHandler):
    """In-process Weaviate stub.

    Class attrs are reset per-test in ``setUp`` so concurrent tests
    don't leak state. The stub answers:
      * GET /v1/schema       → returns ``cls.schema``
      * POST /v1/schema      → appends the body to ``cls.posted``;
                                response 200 unless ``cls.fail_post``.
    """

    schema: dict = {"classes": []}
    posted: list = []
    fail_post: bool = False

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/schema":
            body = json.dumps(self.__class__.schema).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path == "/v1/schema":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                payload = {}
            self.__class__.posted.append(payload)
            if self.__class__.fail_post:
                self.send_response(422)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "class already exists"}')
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):
        pass


def _start_server() -> tuple[http.server.HTTPServer, int, threading.Thread]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return server, port, thread


def _ns(**overrides):
    """Minimal argparse-like namespace for ``_ensure_collections``."""
    ns = type("Ns", (), {})()
    ns.yes = overrides.get("yes", True)
    ns.quiet = overrides.get("quiet", False)
    ns.skip_seed = overrides.get("skip_seed", False)
    ns.skip_collections = overrides.get("skip_collections", False)
    return ns


# ─── Tests ────────────────────────────────────────────────────────────────


class AdoptCaseDifferentSharedKgTests(unittest.TestCase):
    """Adopt mode: existing lowercase-c shared KG should be adopted in
    place when the canonical default is capital-C."""

    def setUp(self):
        # v0.2.89: stub the bounded Weaviate-readiness gate. These tests stub
        # the seed machinery, not the gate; without this, runners with no live
        # Weaviate burn the 150s deadline and raise (and machines WITH one
        # leak a live probe into a hermetic test).
        _gate = mock.patch.object(
            install, "_wait_for_weaviate_ready", lambda *a, **k: True
        )
        _gate.start()
        self.addCleanup(_gate.stop)
        _nonroot = mock.patch.object(install, "_is_orchestrator_root_install", lambda: False)
        _nonroot.start()
        self.addCleanup(_nonroot.stop)
        _Handler.schema = {"classes": []}
        _Handler.posted = []
        _Handler.fail_post = False

    def tearDown(self):
        for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                  "SHARED_KG_COLLECTION", "WEAVIATE_PORT"):
            os.environ.pop(k, None)

    def test_case_different_shared_class_is_never_adopted(self):
        """Pre-seed Weaviate with the lowercase-c v0.2.12–v0.2.22 shared
        KG class, request the canonical capital-C via env. v0.2.103: the
        shared name is NOT rebound to the sibling (that would adopt a class
        without consent) and the capital-C class is not POSTed beside it.
        Red-proof: restore the `_resolve_existing_casing(shared_name)` call
        → the env assertion fails.
        """
        # The on-disk class is the lowercase-c v0.2.12–v0.2.22 default.
        _Handler.schema = {"classes": [
            {"class": "VibecodedOrchestrator_KnowledgeGraph"}
        ]}
        server, port, _ = _start_server()
        try:
            decisions = {"weaviate": {"action": install.ACTION_ADOPT}}
            with mock.patch.dict(
                "os.environ",
                {
                    "WEAVIATE_URL": f"http://localhost:{port}",
                    "WEAVIATE_PORT": str(port),
                    "KG_COLLECTION": "TestKG",
                    "DEVELOPMENT_COLLECTION": "TestDev",
                    # User requests the canonical capital-C (the v0.2.23 B1
                    # default that the source-of-truth constants now use).
                    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
                },
            ):
                install._ensure_collections(
                    {}, decisions=decisions, args=_ns(yes=True),
                )
                self.assertEqual(
                    os.environ["SHARED_KG_COLLECTION"],
                    "VibeCodedOrchestrator_KnowledgeGraph",
                    "the shared name is never rebound to a case-sibling",
                )
            # No capital-C class POSTed beside the lowercase-c sibling.
            posted_classes = [p.get("class") for p in _Handler.posted]
            self.assertNotIn(
                "VibeCodedOrchestrator_KnowledgeGraph",
                posted_classes,
                "must NOT POST a capital-C class beside a lowercase-c sibling",
            )
            # Per-project KG + Dev are still created (they have no
            # case-different siblings here).
            self.assertIn("TestKG", posted_classes)
            self.assertIn("TestDev", posted_classes)
        finally:
            server.shutdown()
            server.server_close()  # L-4: release the listening socket (no ResourceWarning)

    def test_fresh_install_creates_capital_c(self):
        """Empty Weaviate, default env. Fresh install must create the
        capital-C canonical class (no lowercase-c sibling to adopt)."""
        _Handler.schema = {"classes": []}
        server, port, _ = _start_server()
        try:
            with mock.patch.dict(
                "os.environ",
                {
                    "WEAVIATE_URL": f"http://localhost:{port}",
                    "WEAVIATE_PORT": str(port),
                    "KG_COLLECTION": "TestKG",
                    "DEVELOPMENT_COLLECTION": "TestDev",
                    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
                },
            ):
                install._ensure_collections({})
                # Env unchanged (no case-rebind happened — nothing to adopt).
                self.assertEqual(
                    os.environ["SHARED_KG_COLLECTION"],
                    "VibeCodedOrchestrator_KnowledgeGraph",
                )
            posted_classes = [p.get("class") for p in _Handler.posted]
            self.assertIn(
                "VibeCodedOrchestrator_KnowledgeGraph", posted_classes,
                "fresh install must create the capital-C canonical class",
            )
            self.assertNotIn(
                "VibecodedOrchestrator_KnowledgeGraph", posted_classes,
                "fresh install must not create the legacy lowercase-c class",
            )
        finally:
            server.shutdown()
            server.server_close()  # L-4: release the listening socket (no ResourceWarning)

    def test_adopt_mode_skips_when_canonical_already_present(self):
        """When the capital-C canonical class already exists, adopt mode
        is a no-op (exact match, no rebind needed)."""
        _Handler.schema = {"classes": [
            {"class": "VibeCodedOrchestrator_KnowledgeGraph"}
        ]}
        server, port, _ = _start_server()
        try:
            decisions = {"weaviate": {"action": install.ACTION_ADOPT}}
            with mock.patch.dict(
                "os.environ",
                {
                    "WEAVIATE_URL": f"http://localhost:{port}",
                    "WEAVIATE_PORT": str(port),
                    "KG_COLLECTION": "TestKG",
                    "DEVELOPMENT_COLLECTION": "TestDev",
                    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
                },
            ):
                install._ensure_collections(
                    {}, decisions=decisions, args=_ns(yes=True),
                )
                # No rebind (canonical was exact match).
                self.assertEqual(
                    os.environ["SHARED_KG_COLLECTION"],
                    "VibeCodedOrchestrator_KnowledgeGraph",
                )
            posted_classes = [p.get("class") for p in _Handler.posted]
            # Per-project KG + Dev still created; shared is adopted exact-match.
            self.assertIn("TestKG", posted_classes)
            self.assertIn("TestDev", posted_classes)
            self.assertNotIn(
                "VibeCodedOrchestrator_KnowledgeGraph", posted_classes,
                "shared KG already exists (exact match) — must not re-POST",
            )
        finally:
            server.shutdown()
            server.server_close()  # L-4: release the listening socket (no ResourceWarning)

    def test_any_casing_sibling_blocks_but_is_not_adopted(self):
        """ANY case-only sibling (here screaming-snake-case) is neither
        adopted for the shared name nor recreated beside."""
        _Handler.schema = {"classes": [
            {"class": "VIBECODEDORCHESTRATOR_KNOWLEDGEGRAPH"}
        ]}
        server, port, _ = _start_server()
        try:
            decisions = {"weaviate": {"action": install.ACTION_ADOPT}}
            with mock.patch.dict(
                "os.environ",
                {
                    "WEAVIATE_URL": f"http://localhost:{port}",
                    "WEAVIATE_PORT": str(port),
                    "KG_COLLECTION": "TestKG",
                    "DEVELOPMENT_COLLECTION": "TestDev",
                    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
                },
            ):
                install._ensure_collections(
                    {}, decisions=decisions, args=_ns(yes=True),
                )
                self.assertEqual(
                    os.environ["SHARED_KG_COLLECTION"],
                    "VibeCodedOrchestrator_KnowledgeGraph",
                )
            posted_classes = [p.get("class") for p in _Handler.posted]
            self.assertNotIn(
                "VibeCodedOrchestrator_KnowledgeGraph", posted_classes,
                "must not create a class beside its screaming-snake-case sibling",
            )
        finally:
            server.shutdown()
            server.server_close()  # L-4: release the listening socket (no ResourceWarning)


class AdoptCaseDifferentPerProjectKgTests(unittest.TestCase):
    """Adopt mode: per-project KG + Dev collections also get
    case-insensitive adoption (the same generic logic, applied to the
    per-project triple)."""

    def setUp(self):
        # v0.2.89: stub the bounded Weaviate-readiness gate. These tests stub
        # the seed machinery, not the gate; without this, runners with no live
        # Weaviate burn the 150s deadline and raise (and machines WITH one
        # leak a live probe into a hermetic test).
        _gate = mock.patch.object(
            install, "_wait_for_weaviate_ready", lambda *a, **k: True
        )
        _gate.start()
        self.addCleanup(_gate.stop)
        _nonroot = mock.patch.object(install, "_is_orchestrator_root_install", lambda: False)
        _nonroot.start()
        self.addCleanup(_nonroot.stop)
        _Handler.schema = {"classes": []}
        _Handler.posted = []
        _Handler.fail_post = False

    def tearDown(self):
        for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                  "SHARED_KG_COLLECTION", "WEAVIATE_PORT"):
            os.environ.pop(k, None)

    def test_adopt_uses_case_different_per_project_kg(self):
        """A user with a manually-created per-project KG with weird
        casing (e.g. all lowercase) should have it adopted in place."""
        _Handler.schema = {"classes": [
            {"class": "testkg"},  # all-lowercase user-created class
        ]}
        server, port, _ = _start_server()
        try:
            decisions = {"weaviate": {"action": install.ACTION_ADOPT}}
            with mock.patch.dict(
                "os.environ",
                {
                    "WEAVIATE_URL": f"http://localhost:{port}",
                    "WEAVIATE_PORT": str(port),
                    "KG_COLLECTION": "TestKG",  # PascalCase request
                    "DEVELOPMENT_COLLECTION": "TestDev",
                    "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
                },
            ):
                install._ensure_collections(
                    {}, decisions=decisions, args=_ns(yes=True),
                )
                # KG_COLLECTION env rebound to the on-disk casing.
                self.assertEqual(os.environ["KG_COLLECTION"], "testkg")
            posted_classes = [p.get("class") for p in _Handler.posted]
            # No new PascalCase TestKG class POSTed — we adopted "testkg".
            self.assertNotIn(
                "TestKG", posted_classes,
                "must adopt the lowercase 'testkg' instead of creating "
                "PascalCase 'TestKG'",
            )
            # Dev still created (no case-variant of TestDev exists).
            self.assertIn("TestDev", posted_classes)
            # Shared created (no case-variant exists either).
            self.assertIn("VibeCodedOrchestrator_KnowledgeGraph", posted_classes)
        finally:
            server.shutdown()
            server.server_close()  # L-4: release the listening socket (no ResourceWarning)


if __name__ == "__main__":
    unittest.main()
