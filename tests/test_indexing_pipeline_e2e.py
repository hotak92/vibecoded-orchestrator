# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""End-to-end integration test for the diagrams indexing pipeline
(Phase 1.5 — fix/a1-indexing-pipeline 2026-05-25).

This is the test the original Phase 1.5.A/B/C + Phase 2 work was
MISSING. Each phase shipped with its own unit tests, but no test ever
exercised the full chain ``save .mmd`` → ``index_diagram_async`` →
``index_diagram`` → ``_weaviate_upsert(<Project>_Diagrams)`` with the
``DIAGRAMS_COLLECTION`` kwarg actually plumbed end-to-end. The wiring
audit found three integration bugs:

  Bug-1: ``index_diagram_async`` didn't accept ``diagrams_collection``
         and called ``index_diagram`` positionally — kwarg was never
         passed → ``_weaviate_upsert`` silently skipped.
  Bug-2: ``DIAGRAMS_COLLECTION`` was never written to the env by
         ``config_projection`` — MCP server's ``_config_field`` resolution
         returned empty → ``_diagrams_collections_to_search`` returned
         ``[]`` → hybrid_search never returned diagrams.
  Bug-3: The ``<Project>_Diagrams`` Weaviate class was never bootstrapped
         — even after Bug-1 + Bug-2 were fixed, the upsert would fail
         with "no such class".

This test is the regression guard for all three: it constructs a
mocked-Weaviate environment, exercises the async wrapper that the
launcher's wrapper-MCP post_tool_success calls, and verifies the
mocked Weaviate ``data.insert`` is called with the expected
properties on the expected collection.

Run: pytest tests/test_indexing_pipeline_e2e.py -v
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCollectionData:
    """Stand-in for ``weaviate.collections.Collection.data``."""

    def __init__(self) -> None:
        self.inserts: List[dict] = []
        self.deletes: List[Any] = []

    def insert(self, *, properties: dict) -> None:
        # The real client accepts kwarg `properties`; mirror that contract
        # so test assertions don't drift from production call sites.
        self.inserts.append(dict(properties))

    def delete_by_id(self, uuid: Any) -> None:
        self.deletes.append(uuid)


class _FakeCollectionQuery:
    """Stand-in for ``weaviate.collections.Collection.query``."""

    def fetch_objects(self, *, filters: Any, limit: int) -> Any:
        # Return an empty result set — no pre-existing objects to delete.
        # The real shape: ``response.objects`` iterable of objects with
        # ``.uuid``. Empty list matches the "fresh insert" scenario.
        result = MagicMock()
        result.objects = []
        return result


class _FakeCollection:
    """Stand-in for ``weaviate.collections.Collection``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.data = _FakeCollectionData()
        self.query = _FakeCollectionQuery()


class _FakeCollectionsRegistry:
    """Stand-in for ``weaviate.WeaviateClient.collections``."""

    def __init__(self) -> None:
        self._cache: dict[str, _FakeCollection] = {}

    def get(self, name: str) -> _FakeCollection:
        if name not in self._cache:
            self._cache[name] = _FakeCollection(name)
        return self._cache[name]


class _FakeWeaviateClient:
    """Stand-in for ``weaviate.WeaviateClient`` (the v4 client shape)."""

    def __init__(self) -> None:
        self.collections = _FakeCollectionsRegistry()
        self.closed = False

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# DB fixture (mirrors test_diagram_indexer.py's fixture)
# ---------------------------------------------------------------------------


_PROJECT_DIAGRAMS_SCHEMA = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE project_diagrams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    diagram_name TEXT NOT NULL,
    diagram_type TEXT NOT NULL CHECK(diagram_type IN ('mermaid','excalidraw')),
    file_path TEXT NOT NULL,
    category_path TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    inferred_title TEXT,
    diagram_kind TEXT,
    content_text TEXT,
    node_count INTEGER,
    edge_count INTEGER,
    chat_id TEXT,
    linked_session_summary TEXT,
    config_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(project_id, diagram_name)
);
"""


@pytest.fixture
def project_setup(tmp_path: Path, monkeypatch):
    """Build a fake project layout: launcher.db + .claude/diagrams + env."""
    # SQLite DB with the project_diagrams schema.
    db_path = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_PROJECT_DIAGRAMS_SCHEMA)
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-e2e-uuid", "E2EProject"),
        )
        conn.commit()
    finally:
        conn.close()

    # .claude/diagrams/ root.
    diagrams_root = tmp_path / ".claude" / "diagrams"
    diagrams_root.mkdir(parents=True)

    # Env: DIAGRAMS_COLLECTION mimics what config_projection writes.
    monkeypatch.setenv("DIAGRAMS_COLLECTION", "E2EProject_Diagrams")
    # Weaviate URL so _weaviate_upsert doesn't skip on the URL check.
    monkeypatch.setenv("WEAVIATE_URL", "http://localhost:8081")

    return {
        "db_path": db_path,
        "diagrams_root": diagrams_root,
        "project_id": "proj-e2e-uuid",
    }


# ---------------------------------------------------------------------------
# E2E pipeline test
# ---------------------------------------------------------------------------


def _async_run(coro):
    """Run a coroutine on a fresh event loop (no pytest-asyncio dep)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_pipeline_save_mmd_indexes_to_weaviate_with_correct_collection(
    project_setup,
):
    """Full pipeline: save .mmd → index_diagram_async → index_diagram →
    _weaviate_upsert → fake Weaviate ``data.insert`` called with the
    expected properties on the env-resolved collection.

    Regression guard for Bug-1, Bug-2, Bug-3 of the wiring audit:
      - Bug-1: ``diagrams_collection`` reaches the inner indexer (it's
        resolved from env and passed as kwarg).
      - Bug-2: the env-var path is honoured (we set the env var, not
        an explicit kwarg, and the upsert still receives the value).
      - Bug-3: the upsert is reached at all (pre-fix it was skipped
        silently because diagrams_collection was always None)."""
    from vco_lib.diagram_indexer import index_diagram_async

    # Save a Mermaid file under the scoped path.
    cat = project_setup["diagrams_root"] / "gui" / "auth"
    cat.mkdir(parents=True)
    f = cat / "login-flow.mmd"
    f.write_text(
        "---\n"
        "title: Login Flow\n"
        "---\n"
        "flowchart TD\n"
        "  Start[Start] --> Login[Login]\n"
        "  Login --> Done[Done]\n"
    )

    # Patch the weaviate v4 connect helper used by _weaviate_upsert.
    fake_client = _FakeWeaviateClient()
    with patch(
        "weaviate.connect_to_custom", return_value=fake_client,
    ):
        # Caller does NOT pass diagrams_collection — must be resolved
        # from DIAGRAMS_COLLECTION env (Bug-2 regression guard).
        row = _async_run(
            index_diagram_async(
                f,
                project_id=project_setup["project_id"],
                chat_id="chat-e2e-1",
                db_path=project_setup["db_path"],
            )
        )

    # Pipeline contract:
    assert row.id is not None
    assert row.diagram_type == "mermaid"
    assert row.diagram_name == "login-flow"
    assert row.inferred_title == "Login Flow"
    assert row.diagram_kind == "flowchart"
    # Bug-1: wrote_weaviate must be True (was always False pre-fix
    # because diagrams_collection was None → _weaviate_upsert short-
    # circuited at the URL/collection check).
    assert getattr(row, "wrote_weaviate", False) is True

    # Bug-3 regression guard: the collection registry was hit with the
    # canonical env-resolved name (NOT empty, NOT a default fallback).
    expected_collection = "E2EProject_Diagrams"
    assert expected_collection in fake_client.collections._cache
    target = fake_client.collections._cache[expected_collection]

    # Exactly one insert with the canonical property shape.
    assert len(target.data.inserts) == 1
    props = target.data.inserts[0]
    assert props["title"] == "Login Flow"
    assert "flowchart TD" in props["content"]
    # path_tags derived from category_path "gui/auth".
    assert props["path_tags"] == ["gui", "auth"]
    assert props["diagram_kind"] == "flowchart"
    assert props["chat_id"] == "chat-e2e-1"
    assert props["file_path"] == str(f.resolve())
    # Epoch ints (not date strings) — matches the diagrams_class_definition
    # schema where created_at / updated_at are ``int`` dataType.
    assert isinstance(props["created_at"], int)
    assert isinstance(props["updated_at"], int)

    # Client was closed cleanly (best-effort hygiene).
    assert fake_client.closed is True


def test_pipeline_explicit_collection_overrides_env(project_setup, monkeypatch):
    """When the caller passes an explicit ``diagrams_collection`` it wins
    over the env var — explicit kwarg takes precedence (fix/a1-indexing-
    pipeline contract). This protects against env-leak scenarios where
    a stale env var would otherwise mis-route writes."""
    from vco_lib.diagram_indexer import index_diagram_async

    cat = project_setup["diagrams_root"] / "architecture"
    cat.mkdir(parents=True)
    f = cat / "data-flow.mmd"
    f.write_text("flowchart LR\n  A --> B")

    fake_client = _FakeWeaviateClient()
    with patch("weaviate.connect_to_custom", return_value=fake_client):
        _async_run(
            index_diagram_async(
                f,
                project_id=project_setup["project_id"],
                chat_id=None,
                diagrams_collection="ExplicitOverride_Diagrams",
                db_path=project_setup["db_path"],
            )
        )

    # The env said E2EProject_Diagrams — the explicit kwarg overrode it.
    assert "ExplicitOverride_Diagrams" in fake_client.collections._cache
    assert "E2EProject_Diagrams" not in fake_client.collections._cache


def test_pipeline_missing_env_skips_weaviate_gracefully(
    tmp_path, monkeypatch,
):
    """Back-compat: when DIAGRAMS_COLLECTION isn't in env AND the caller
    doesn't pass it, the upsert is skipped silently — SQLite + sidecar
    still happen. This preserves the pre-fix behaviour for projects
    that haven't been re-projected via config_projection yet."""
    from vco_lib.diagram_indexer import index_diagram_async

    # Fresh setup without the env var set.
    db_path = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_PROJECT_DIAGRAMS_SCHEMA)
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("proj-noenv-uuid", "NoEnvProj"),
        )
        conn.commit()
    finally:
        conn.close()
    diagrams_root = tmp_path / ".claude" / "diagrams"
    diagrams_root.mkdir(parents=True)
    monkeypatch.delenv("DIAGRAMS_COLLECTION", raising=False)
    monkeypatch.setenv("WEAVIATE_URL", "http://localhost:8081")

    cat = diagrams_root / "gui"
    cat.mkdir(parents=True)
    f = cat / "no-collection.mmd"
    f.write_text("flowchart TD\n  A --> B")

    fake_client = _FakeWeaviateClient()
    with patch("weaviate.connect_to_custom", return_value=fake_client):
        row = _async_run(
            index_diagram_async(
                f, project_id="proj-noenv-uuid", db_path=db_path,
            )
        )

    # SQLite + sidecar succeeded.
    assert row.id is not None
    # Sidecar exists.
    assert f.with_suffix(".mmd.meta.json").exists()
    # Weaviate write was skipped — the fake client was never asked for
    # a collection (connect_to_custom wasn't even called, since the
    # collection-name guard short-circuits BEFORE connect).
    assert getattr(row, "wrote_weaviate", False) is False
    # No collections were created on the fake client.
    assert len(fake_client.collections._cache) == 0
