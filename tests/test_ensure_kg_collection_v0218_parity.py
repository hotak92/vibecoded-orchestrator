# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.18 KG-collection runtime-fallback creator parity
(cleanup commit, 2026-05-19).

Mirrors `test_dev_collection_v0218_parity.py::EnsureDevCollectionV0218Tests`
for the KG variant. The runtime-fallback `ensure_collection_exists` in
`templates/scripts/sync_knowledge_graph.py` previously hardcoded the
legacy 3-slot vector config (qwen3 + ollama + openai). v0.2.18 sources
the slot catalog from `vco_lib.weaviate_schema.KG_NAMED_VECTORS` so this
runtime path matches `vco_lib.project_init.kg_class_definition` exactly,
preventing the migrate dispatcher from looping on a phantom missing-slot
diff.

Run: pytest tests/test_ensure_kg_collection_v0218_parity.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.weaviate_schema import KG_NAMED_VECTORS  # noqa: E402


def _load_sync_module(test_id: int):
    """Load `templates/scripts/sync_knowledge_graph.py` as a module
    without executing its __main__ guard. Each test gets a unique
    sys.modules entry so module-level mutations don't leak."""
    os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
    script_path = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
    mod_name = f"_test_kg_parity_sync_{test_id}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EnsureKgCollectionV0218Tests(unittest.TestCase):
    """Runtime fallback creator must match `kg_class_definition` exactly."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._test_id = id(cls)
        cls._mod = _load_sync_module(cls._test_id)

    def _make_server_with_no_existing_collection(
        self, kg_name: str = "Foo_KnowledgeGraph",
    ):
        """Build a server stand-in where `collections.exists(...)`
        returns False (so the create branch fires)."""
        srv = MagicMock()
        srv.client.collections.exists.return_value = False
        srv.client.collections.create = MagicMock()
        # Module reads COLLECTION_NAME at module-level; patch it in the
        # loaded module's namespace.
        self._mod.COLLECTION_NAME = kg_name
        return srv

    def test_ensure_kg_collection_uses_KG_NAMED_VECTORS_slots(self):
        """All 5 v0.2.18 slots from `KG_NAMED_VECTORS` must be configured.

        Pre-v0.2.18 this hardcoded 3 slots (qwen3 + ollama + openai).
        v0.2.18 cleanup sources from the canonical catalog so the
        runtime fallback matches `project_init.kg_class_definition`
        exactly. Same pattern as `ensure_dev_collection_exists` landed
        at bcacfc0.
        """
        srv = self._make_server_with_no_existing_collection()
        ok = self._mod.ensure_collection_exists(srv)
        self.assertTrue(ok)

        srv.client.collections.create.assert_called_once()
        kwargs = srv.client.collections.create.call_args.kwargs
        vec_config = kwargs.get("vectorizer_config", [])

        # Each Configure.NamedVectors.none(name=X) carries the slot name
        # as `.name` on weaviate-py v4. Extract defensively.
        configured = set()
        for entry in vec_config:
            n = getattr(entry, "name", None)
            if n is not None:
                configured.add(n)
            else:
                configured.add(str(entry))

        expected = {slot.name for slot in KG_NAMED_VECTORS}
        # Defensive: at minimum all 5 v0.2.18 slots must be present.
        # No no-extras assertion — future v0.2.19 may legitimately add
        # slots this commit's tests don't know about.
        for slot_name in expected:
            self.assertTrue(
                any(
                    slot_name in c
                    for c in (
                        configured
                        if all(isinstance(x, str) for x in configured)
                        else [s for s in configured]
                    )
                ),
                f"missing slot '{slot_name}' in ensure_collection_exists "
                f"vectorizer_config; saw: {sorted(configured)}",
            )
        # Stronger assertion when introspection worked cleanly: we expect
        # >= 5 distinct slot names (the v0.2.18 KG catalog).
        if all(isinstance(x, str) for x in configured):
            self.assertGreaterEqual(
                len(configured), 5,
                f"v0.2.18 expects >= 5 named-vector slots; saw "
                f"{len(configured)}: {sorted(configured)}",
            )

    def test_ensure_kg_collection_create_branch_fires_with_expected_props(self):
        """Smoke test: the create path passes the v0.2.18 property set.

        Guards against the `return True` accidentally moving above the
        `collections.create(...)` call (which would silently skip
        collection creation in the no-exists branch).
        """
        srv = self._make_server_with_no_existing_collection()
        ok = self._mod.ensure_collection_exists(srv)
        self.assertTrue(ok)

        srv.client.collections.create.assert_called_once()
        kwargs = srv.client.collections.create.call_args.kwargs
        props = kwargs.get("properties", [])
        prop_names = {p.name for p in props}
        # Spot-check the load-bearing prop names (full schema parity is
        # asserted elsewhere in `test_kg_class_definition_v0218*` once
        # those land — this test just confirms the create call fires
        # with the right shape).
        for required in (
            "title", "content", "file_path", "node_type",
            "tags", "links", "typed_links", "external_links",
            "content_hash", "status",
            "created", "updated", "valid_from", "valid_until",
        ):
            self.assertIn(
                required, prop_names,
                f"ensure_collection_exists must pass `{required}` to create()",
            )


if __name__ == "__main__":
    unittest.main()
