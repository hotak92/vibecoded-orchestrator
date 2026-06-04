# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.46 Decision B — per-project ``SHARED_KG_READ_DISABLED`` opt-out.

Symmetric mirror of the existing ``SHARED_KG_WRITE_DISABLED`` gate.
When ``true``, the MCP's ``_kg_collections_to_search`` drops
``SHARED_KG_COLLECTION`` from the hybrid_search /
semantic_graph_search fan-out so the project stops searching the
shared corpus.

Pre-v0.2.46 the read path was unconditional (asymmetric access
model); v0.2.46 lets users opt OUT explicitly while keeping the
default ON. Asymmetric-by-default remains: fresh projects READ +
WRITE the shared KG; users who want strict isolation flip both
flags.

These tests cover the four propagation surfaces touched by Decision B:
  1. ``claude_mcp_servers/weaviate_mcp/server.py::
     _resolve_shared_kg_read_disabled`` (env-var resolution).
  2. ``_kg_collections_to_search`` gate (the single-line drop).
  3. ``vco_lib.config_projection`` (DB → env-file emit).
  4. ``vco_lib.env_template`` (canonical-key membership).

Coverage shape mirrors ``test_kg_collection_env_backfill.py`` (the
audit-cited reference): a minimal launcher.db fixture + envvar
monkeypatch for the MCP-side resolver.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ─────────────────────────────────────────────────────────────────────
# Helpers — minimal launcher.db fixture mirroring the write-disable shape
# ─────────────────────────────────────────────────────────────────────


def _make_project(tmp: Path) -> Path:
    folder = tmp / "fake-project"
    (folder / ".claude").mkdir(parents=True, exist_ok=True)
    (folder / ".claude" / "settings.json").write_text(
        json.dumps({"$schema": "ignored", "permissions": {"allow": []}}, indent=2) + "\n",
        encoding="utf-8",
    )
    return folder


def _make_launcher_db_with_read_disabled(
    state_dir: Path,
    project_folder: Path,
    *,
    primary: str,
    shared: str | None,
    read_disabled: bool | None,
) -> Path:
    """Create a minimal launcher.db with a project row, primary/shared
    bindings, and (optionally) a `module_settings` row carrying the
    ``shared_kg_read_disabled`` flag for orchestrator-core. Pass
    ``read_disabled=None`` to skip seeding the row entirely (exercises
    the default-on-missing-row path)."""
    db_path = state_dir / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL UNIQUE,
            host TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            slug TEXT
        );
        CREATE TABLE project_kg_bindings (
            project_id TEXT NOT NULL,
            role TEXT NOT NULL,
            collection_name TEXT NOT NULL,
            embedding_model TEXT,
            embedding_dim INTEGER,
            kg_dir_path TEXT,
            weaviate_url TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (project_id, role)
        );
        CREATE TABLE module_settings (
            project_id TEXT NOT NULL,
            module_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (project_id, module_id, setting_key)
        );
        """
    )
    pid = "00000000-0000-0000-0000-000000000001"
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, host, created_at, updated_at, slug) "
        "VALUES (?, ?, ?, ?, 0, 0, ?)",
        (pid, "Test", str(project_folder.resolve()), "base", "test"),
    )
    cur.execute(
        "INSERT INTO project_kg_bindings "
        "(project_id, role, collection_name, config_json, updated_at) "
        "VALUES (?, 'primary', ?, '{}', 0)",
        (pid, primary),
    )
    if shared is not None:
        cur.execute(
            "INSERT INTO project_kg_bindings "
            "(project_id, role, collection_name, config_json, updated_at) "
            "VALUES (?, 'shared', ?, '{}', 0)",
            (pid, shared),
        )
    if read_disabled is not None:
        # Boolean → JSON-encoded value (matches Rust's
        # `serde_json::Value::Bool` shape used by `db.set_setting`).
        cur.execute(
            "INSERT INTO module_settings "
            "(project_id, module_id, setting_key, setting_value, updated_at) "
            "VALUES (?, 'orchestrator-core', 'shared_kg_read_disabled', ?, 0)",
            (pid, json.dumps(bool(read_disabled))),
        )
    conn.commit()
    conn.close()
    return db_path


# ─────────────────────────────────────────────────────────────────────
# MCP-side resolver — env var honoured by ``_resolve_shared_kg_read_disabled``
# ─────────────────────────────────────────────────────────────────────


class McpEnvResolverTests(unittest.TestCase):
    """Direct unit test of ``_resolve_shared_kg_read_disabled``.

    The helper lives at the top of ``weaviate_mcp/server.py``; we
    import it lazily so the test doesn't pay the MCP startup cost.
    """

    def _import_resolver(self):
        # Importing the full server module is too heavy (it boots the
        # MCP runtime). Use the source-level definition via importlib's
        # spec-loader on a single-function shim.
        from claude_mcp_servers.weaviate_mcp import server  # type: ignore
        return server._resolve_shared_kg_read_disabled

    def test_default_no_env_var_returns_false(self):
        resolver = self._import_resolver()
        with mock.patch.dict("os.environ", {}, clear=False):
            # Ensure no inherited value bleeds through.
            import os
            os.environ.pop("SHARED_KG_READ_DISABLED", None)
            self.assertFalse(resolver())

    def test_explicit_true_returns_true(self):
        resolver = self._import_resolver()
        for spelling in ("true", "True", "1", "yes", "YES"):
            with self.subTest(spelling=spelling):
                with mock.patch.dict(
                    "os.environ",
                    {"SHARED_KG_READ_DISABLED": spelling},
                    clear=False,
                ):
                    self.assertTrue(resolver(), f"value={spelling!r}")

    def test_explicit_false_returns_false(self):
        resolver = self._import_resolver()
        for spelling in ("false", "False", "0", "no", ""):
            with self.subTest(spelling=spelling):
                with mock.patch.dict(
                    "os.environ",
                    {"SHARED_KG_READ_DISABLED": spelling},
                    clear=False,
                ):
                    self.assertFalse(resolver(), f"value={spelling!r}")


# ─────────────────────────────────────────────────────────────────────
# MCP-side gate — ``_kg_collections_to_search`` drops shared when flag set
# ─────────────────────────────────────────────────────────────────────


class McpFanOutGateTests(unittest.TestCase):
    """Verify ``_kg_collections_to_search`` honours the runtime constant.

    The module-level ``SHARED_KG_READ_DISABLED`` constant is evaluated
    at import time, so we patch it directly on the module object for
    the duration of each test (no re-import flap).
    """

    def _import_server(self):
        from claude_mcp_servers.weaviate_mcp import server  # type: ignore
        return server

    def test_shared_in_fanout_by_default(self):
        server = self._import_server()
        with mock.patch.object(server, "KG_COLLECTION", "MyProject_KnowledgeGraph"), \
             mock.patch.object(server, "SHARED_KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph"), \
             mock.patch.object(server, "SHARED_KG_READ_DISABLED", False), \
             mock.patch.object(server, "_kg_peer_collections", lambda: []):
            out = server._kg_collections_to_search()
        self.assertIn("VibeCodedOrchestrator_KnowledgeGraph", out)
        self.assertIn("MyProject_KnowledgeGraph", out)

    def test_shared_dropped_when_read_disabled(self):
        server = self._import_server()
        with mock.patch.object(server, "KG_COLLECTION", "MyProject_KnowledgeGraph"), \
             mock.patch.object(server, "SHARED_KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph"), \
             mock.patch.object(server, "SHARED_KG_READ_DISABLED", True), \
             mock.patch.object(server, "_kg_peer_collections", lambda: []):
            out = server._kg_collections_to_search()
        self.assertIn("MyProject_KnowledgeGraph", out)
        self.assertNotIn(
            "VibeCodedOrchestrator_KnowledgeGraph",
            out,
            "shared collection must drop out when SHARED_KG_READ_DISABLED=true",
        )

    def test_peer_collections_still_searched_when_read_disabled(self):
        """The READ gate only affects the SHARED collection — peer
        projects granted via the access matrix remain in the fan-out.
        """
        server = self._import_server()
        with mock.patch.object(server, "KG_COLLECTION", "MyProject_KnowledgeGraph"), \
             mock.patch.object(server, "SHARED_KG_COLLECTION", "VibeCodedOrchestrator_KnowledgeGraph"), \
             mock.patch.object(server, "SHARED_KG_READ_DISABLED", True), \
             mock.patch.object(
                 server, "_kg_peer_collections",
                 lambda: ["Peer1_KnowledgeGraph", "Peer2_KnowledgeGraph"],
             ):
            out = server._kg_collections_to_search()
        self.assertIn("Peer1_KnowledgeGraph", out)
        self.assertIn("Peer2_KnowledgeGraph", out)
        self.assertNotIn("VibeCodedOrchestrator_KnowledgeGraph", out)


# ─────────────────────────────────────────────────────────────────────
# Python contract — config_projection emits SHARED_KG_READ_DISABLED
# ─────────────────────────────────────────────────────────────────────


class ConfigProjectionEmitTests(unittest.TestCase):
    """v0.2.46 Decision B — ``vco_lib.config_projection`` registers
    ``SHARED_KG_READ_DISABLED`` in ``_CANONICAL_KEYS`` and the
    ``project_env_from_db`` resolver emits it via ``_set``.

    Pinned without spinning up a full DB to keep the unit footprint
    small: the canonical-keys membership is a closed set surfaced via
    ``list_canonical_keys``, and the env-template subset invariant
    enforces the cross-module link at import time.
    """

    def test_shared_kg_read_disabled_in_canonical_keys(self):
        from vco_lib import config_projection
        keys = config_projection.list_canonical_keys()
        self.assertIn("SHARED_KG_READ_DISABLED", keys)

    def test_subset_invariant_holds(self):
        """``env_template._CANONICAL_ENV_TEMPLATE_KEYS`` must be a
        subset of ``config_projection._CANONICAL_KEYS``. Re-import
        ``env_template`` to re-trigger the import-time assertion;
        if it raises, the canonical-keys list is missing the new key.
        """
        from vco_lib import config_projection, env_template
        importlib.reload(config_projection)
        importlib.reload(env_template)
        full = config_projection.list_canonical_keys()
        template = env_template.list_canonical_env_template_keys()
        self.assertTrue(
            template.issubset(full),
            f"env_template keys not a subset of config_projection. "
            f"Offending: {template - full}",
        )

    def test_shared_kg_read_disabled_in_env_template_keys(self):
        from vco_lib import env_template
        keys = env_template.list_canonical_env_template_keys()
        self.assertIn("SHARED_KG_READ_DISABLED", keys)


# ─────────────────────────────────────────────────────────────────────
# ProjectConfig parser back-compat — pre-v0.2.46 hubs omit the field
# ─────────────────────────────────────────────────────────────────────


class ProjectConfigParserTests(unittest.TestCase):
    """The hub-response parser at ``vco_lib.project_config._from_hub_body``
    must default ``shared_kg_read_disabled`` to ``False`` when the field
    is absent (pre-v0.2.46 hub paired with v0.2.46+ client) and surface
    the value verbatim when present.
    """

    def _minimal_body(self, **extras) -> dict:
        # Minimal body covering every REQUIRED field of ProjectConfig.
        # ``shared_kg_read_disabled`` is OPTIONAL (additive in v0.2.46);
        # the parser must back-fill on absence.
        body: dict = {
            "schema_version": 1,
            "project_id": "p1",
            "project_path": "/tmp/p1",
            "project_slug": "p1",
            "project_display_name": "P1",
            "code_graph_project": "p1",
            "code_graph_collection_prefix": "P1",
            "kg_collection": "P1_KnowledgeGraph",
            "shared_kg_collection": "VibeCodedOrchestrator_KnowledgeGraph",
            "development_collection": "P1_Development",
            "active_embedding": "qwen3",
            "embedding_models": {
                "text": "qwen3-embedding:0.6b",
                "code": "CodeSage-Large-v2",
            },
            "kg_access_list": [],
            "codegraph_access_list": ["p1"],
            "weaviate_url": "http://localhost:8081",
            "ollama_url": "http://localhost:11435",
            "grpc_port": 50052,
            "shared_kg_write_disabled": False,
        }
        body.update(extras)
        return body

    def test_absent_field_defaults_to_false(self):
        from vco_lib import project_config
        body = self._minimal_body()
        # Body deliberately OMITS shared_kg_read_disabled — pre-v0.2.46 hub.
        cfg = project_config._from_hub_body(body)
        self.assertFalse(
            cfg.shared_kg_read_disabled,
            "absent field must default to False so pre-v0.2.46 hubs don't crash new clients",
        )

    def test_explicit_true_propagates(self):
        from vco_lib import project_config
        body = self._minimal_body(shared_kg_read_disabled=True)
        cfg = project_config._from_hub_body(body)
        self.assertTrue(cfg.shared_kg_read_disabled)

    def test_explicit_false_propagates(self):
        from vco_lib import project_config
        body = self._minimal_body(shared_kg_read_disabled=False)
        cfg = project_config._from_hub_body(body)
        self.assertFalse(cfg.shared_kg_read_disabled)


if __name__ == "__main__":
    unittest.main()
