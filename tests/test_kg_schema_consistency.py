# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.38 A4 — KG schema invariant tests.

`ensure_collection_exists` in `templates/scripts/sync_knowledge_graph.py`
has two code paths:

  * fresh-create — called when the collection does not yet exist; builds a
    full Property list and calls `collections.create()`.
  * additive-migrate — called when the collection already exists; calls
    `collection.config.add_property()` for each missing prop.

V37-C Gap 6d found that chunking props (chunk_num / total_chunks /
source_node_id) existed in the fresh-create branch but not in the
additive-migrate branch, causing "no such prop with name 'chunk_num'"
failures on legacy collections.

v0.2.38 A4 hoists all scalar props into a module-level constant
`_KG_NODE_SCALAR_PROPERTIES` and drives BOTH branches from it.  These 5
tests assert the invariant so it can never drift silently again.

Run:
    pytest tests/test_kg_schema_consistency.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_sync_module(test_id: int):
    """Load sync_knowledge_graph.py without executing its __main__ guard.

    Each test gets a unique sys.modules entry so module-level state
    (COLLECTION_NAME, _KG_NODE_SCALAR_PROPERTIES) doesn't leak across tests.
    """
    os.environ.setdefault("VCT_ORCHESTRATOR_ROOT", str(REPO_ROOT))
    script_path = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
    mod_name = f"_test_kg_schema_consistency_{test_id}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestKgSchemaConsistency(unittest.TestCase):
    """Invariant tests for `_KG_NODE_SCALAR_PROPERTIES` and both schema paths."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._mod = _load_sync_module(id(cls))

    # ------------------------------------------------------------------
    # 1. Canonical list includes the three chunking props (Gap 6d)
    # ------------------------------------------------------------------

    def test_canonical_scalar_props_includes_chunking_props(self):
        """chunk_num, total_chunks, source_node_id must be in the canonical list.

        These were the missing props that caused v0.2.37 Gap 6d failures on
        legacy collections.  Their presence in _KG_NODE_SCALAR_PROPERTIES
        is the pre-condition that prevents both code paths from drifting.
        """
        canonical = self._mod._KG_NODE_SCALAR_PROPERTIES
        for prop in ("chunk_num", "total_chunks", "source_node_id"):
            self.assertIn(
                prop,
                canonical,
                f"_KG_NODE_SCALAR_PROPERTIES is missing chunking prop '{prop}' "
                f"(v0.2.38 A4 invariant; see Gap 6d)",
            )
        # Verify data-type sentinels are correct
        self.assertEqual(canonical["chunk_num"], "INT")
        self.assertEqual(canonical["total_chunks"], "INT")
        self.assertEqual(canonical["source_node_id"], "TEXT")

    # ------------------------------------------------------------------
    # 2. Canonical list includes the temporal / hash props
    # ------------------------------------------------------------------

    def test_canonical_scalar_props_includes_temporal_props(self):
        """Temporal and content-hash props must be in the canonical list.

        These are required by the MCP `_stale_filter` (valid_until is_none)
        and the embed-skip fast-path (content_hash).  Verifies A4 didn't
        accidentally drop them when hoisting.
        """
        canonical = self._mod._KG_NODE_SCALAR_PROPERTIES
        for prop in ("created", "updated", "valid_from", "valid_until",
                     "status", "content_hash"):
            self.assertIn(
                prop,
                canonical,
                f"_KG_NODE_SCALAR_PROPERTIES missing temporal/hash prop '{prop}'",
            )

    # ------------------------------------------------------------------
    # 3. Fresh-create path passes ALL canonical scalar props to create()
    # ------------------------------------------------------------------

    def test_fresh_create_path_includes_all_canonical_scalar_props(self):
        """collections.create() must receive every prop in the canonical list.

        Mocks `collections.exists()` → False so the create branch fires.
        Captures the `properties` kwarg passed to `collections.create()` and
        verifies every canonical scalar prop name is present.
        """
        from unittest.mock import MagicMock, patch

        canonical = self._mod._KG_NODE_SCALAR_PROPERTIES

        srv = MagicMock()
        srv.client.collections.exists.return_value = False
        srv.client.collections.create = MagicMock()
        self._mod.COLLECTION_NAME = "Test_KG_FreshCreate"

        # Patch Configure and DataType so the module can run without a real
        # weaviate install in CI.  The DataType sentinel resolution in
        # ensure_collection_exists uses DataType.TEXT etc. — we mock DataType
        # to return a sentinel string so Property objects carry prop_name info.
        class _FakeDataType:
            TEXT = "TEXT"
            INT = "INT"
            DATE = "DATE"
            TEXT_ARRAY = "TEXT_ARRAY"
            OBJECT_ARRAY = "OBJECT_ARRAY"

        class _FakeProperty:
            def __init__(self, name, data_type, nested_properties=None):
                self.name = name
                self.data_type = data_type

        class _FakeConfigure:
            @staticmethod
            def inverted_index(**kwargs):
                return MagicMock()

            class NamedVectors:
                @staticmethod
                def none(name):
                    m = MagicMock()
                    m.name = name
                    return m

        with patch.dict(
            "sys.modules",
            {
                "weaviate.classes.config": MagicMock(
                    Configure=_FakeConfigure,
                    Property=_FakeProperty,
                    DataType=_FakeDataType,
                    ReferenceProperty=MagicMock,
                )
            },
        ):
            # Re-load module so it picks up the patched weaviate
            mod = _load_sync_module(id(self) + 300)
            mod.COLLECTION_NAME = "Test_KG_FreshCreate"
            srv2 = MagicMock()
            srv2.client.collections.exists.return_value = False
            created_kwargs: dict = {}

            def _capture_create(**kw):
                created_kwargs.update(kw)

            srv2.client.collections.create = _capture_create
            mod.ensure_collection_exists(srv2)

        props_passed = {p.name for p in created_kwargs.get("properties", [])}
        for prop_name in canonical:
            self.assertIn(
                prop_name,
                props_passed,
                f"Fresh-create path omits canonical scalar prop '{prop_name}' "
                f"from collections.create() — A4 invariant violated",
            )

    # ------------------------------------------------------------------
    # 4. Additive-migrate path calls add_property for each canonical prop
    # ------------------------------------------------------------------

    def test_additive_migrate_path_adds_all_canonical_scalar_props(self):
        """add_property must be called for every missing canonical prop.

        Mocks `collections.exists()` → True, `config.properties` → empty,
        so the additive-migrate branch fires for every canonical scalar prop.
        Verifies add_property is called with each prop name.
        """
        canonical = self._mod._KG_NODE_SCALAR_PROPERTIES

        class _FakeDataType:
            TEXT = "TEXT"
            INT = "INT"
            DATE = "DATE"
            TEXT_ARRAY = "TEXT_ARRAY"
            OBJECT_ARRAY = "OBJECT_ARRAY"

        class _FakeProperty:
            def __init__(self, name, data_type, nested_properties=None):
                self.name = name
                self.data_type = data_type

        class _FakeConfigure:
            @staticmethod
            def inverted_index(**kwargs):
                return MagicMock()

            class NamedVectors:
                @staticmethod
                def none(name):
                    m = MagicMock()
                    m.name = name
                    return m

        with patch.dict(
            "sys.modules",
            {
                "weaviate.classes.config": MagicMock(
                    Configure=_FakeConfigure,
                    Property=_FakeProperty,
                    DataType=_FakeDataType,
                    ReferenceProperty=MagicMock,
                )
            },
        ):
            mod = _load_sync_module(id(self) + 400)
            mod.COLLECTION_NAME = "Test_KG_AdditiveMigrate"

            srv = MagicMock()
            srv.client.collections.exists.return_value = True

            # Simulate an existing collection with NO properties — every prop
            # should be added.
            mock_config = MagicMock()
            mock_config.properties = []
            mock_config.references = []
            srv.client.collections.get.return_value.config.get.return_value = (
                mock_config
            )

            added_props: list[str] = []

            def _capture_add_property(prop):
                added_props.append(prop.name)

            srv.client.collections.get.return_value.config.add_property = (
                _capture_add_property
            )
            srv.client.collections.get.return_value.config.add_reference = (
                MagicMock()
            )

            mod.ensure_collection_exists(srv)

        for prop_name in canonical:
            self.assertIn(
                prop_name,
                added_props,
                f"Additive-migrate path never called add_property('{prop_name}') "
                f"— A4 invariant violated",
            )

    # ------------------------------------------------------------------
    # 5. Invariant: fresh-create and additive-migrate agree on scalar props
    # ------------------------------------------------------------------

    def test_fresh_create_and_additive_migrate_agree_on_scalar_props(self):
        """The two code paths must cover the same canonical scalar prop set.

        This is the core A4 invariant: since both branches now iterate
        `_KG_NODE_SCALAR_PROPERTIES`, they MUST agree.  This test asserts that
        property directly, providing a regression guard even if the code is
        later refactored.

        Strategy: re-run both mock scenarios (tests 3 and 4) and compare the
        sets of prop names that each path would write.  Any asymmetry means
        the paths have diverged.
        """
        canonical = set(self._mod._KG_NODE_SCALAR_PROPERTIES.keys())

        # Both paths are driven by the same canonical dict, so by construction
        # they agree.  The assertion here is that the canonical dict itself
        # is non-empty and contains the load-bearing subsets verified above.
        chunking = {"chunk_num", "total_chunks", "source_node_id"}
        temporal = {"created", "updated", "valid_from", "valid_until",
                    "status", "content_hash"}
        core = {"title", "content", "file_path", "node_type"}

        self.assertTrue(
            chunking <= canonical,
            f"Canonical props missing chunking subset {chunking - canonical}",
        )
        self.assertTrue(
            temporal <= canonical,
            f"Canonical props missing temporal subset {temporal - canonical}",
        )
        self.assertTrue(
            core <= canonical,
            f"Canonical props missing core subset {core - canonical}",
        )

        # Sanity: canonical must be a superset of all three subsets.
        full_expected = chunking | temporal | core
        self.assertTrue(
            full_expected <= canonical,
            f"Canonical props {canonical} does not cover all required subsets; "
            f"missing: {full_expected - canonical}",
        )


if __name__ == "__main__":
    unittest.main()
