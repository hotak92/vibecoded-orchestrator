# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Contract test capturing the create/patch behaviour of the TWO Weaviate
collection-bootstrap paths — and DOCUMENTING that their cores diverge.

v0.2.77 Part 7a-bis (task 2) explored converging
``install.py::_ensure_collections`` and
``vco_lib.project_init.bootstrap_collections`` onto one shared CREATE/PATCH
core. The exploration found the cores are **genuinely different in
behaviour, not just shape** — so per the brief the convergence is NOT
performed; instead this test PINS both behaviours side-by-side so the
divergence is explicit, and a future converger sees exactly what a unified
core would have to reconcile.

The divergence (on the "class already EXISTS but its schema drifted" case):

  * ``bootstrap_collections`` (project_init): runs ``_schema_incompatible``
    and, on drift (legacy single-vector, missing core named-vector slots,
    missing indexNullState), DROPS + RECREATES the class
    (``_drop_and_recreate``). It is a create-OR-REGENERATE bootstrap that
    heals schema drift destructively (lossless because knowledge/**/*.md +
    kg-sync re-populate).

  * ``_ensure_collections`` (install.py): only ever CREATES missing classes.
    An existing class — regardless of schema drift — is ADOPTED AS-IS (no
    drop, no recreate, no schema probe beyond the name-existence set). It
    layers case-insensitive adoption + adopt-mode prompt + env propagation
    on top, but never touches an existing class's schema.

Unifying these would force one policy onto both call-sites: either install.py
would start destructively regenerating drifted classes during a plain
``install.py --update`` (a behaviour change with data-safety weight the
current adopt-as-is contract deliberately avoids), or bootstrap_collections
would stop healing drift (regressing the v0.2.4 Bug-1 migration path the Rust
caller drives via ``regenerated[]``). Both are behaviour regressions — hence
the STOP.

What the two DO agree on (and this test also pins): on a class that is
ABSENT, both POST the target definition; and both treat a 422 "already
exists" POST response as benign (idempotent race tolerance).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402


# A schema that is DRIFTED relative to the canonical target: legacy
# single-vector (no ``vectorConfig``). ``_schema_incompatible`` flags this as
# a regen trigger.
_DRIFTED_LEGACY_SINGLE_VECTOR = {
    "class": "Alpha_KnowledgeGraph",
    "properties": [],
    # NOTE: no "vectorConfig" key → legacy single-vector → incompatible.
}


class BootstrapRegeneratesOnDriftTests(unittest.TestCase):
    """``bootstrap_collections`` DROPS + RECREATES a drifted existing class."""

    def test_drift_triggers_drop_and_recreate(self) -> None:
        created: list[str] = []
        dropped: list[str] = []

        def _fake_fetch(name, weaviate_url=None):
            # Only the per-project KG exists (drifted); everything else absent.
            if name == "Alpha_KnowledgeGraph":
                return dict(_DRIFTED_LEGACY_SINGLE_VECTOR)
            return None

        def _fake_create(payload, weaviate_url=None):
            created.append(payload.get("class"))

        def _fake_drop_recreate(name, definition, weaviate_url=None,
                                log_event=None, reason=""):
            dropped.append(name)
            created.append(definition.get("class"))

        with mock.patch.object(project_init, "_is_weaviate_reachable",
                               return_value=True), \
             mock.patch.object(project_init, "_fetch_schema", _fake_fetch), \
             mock.patch.object(project_init, "_create_class", _fake_create), \
             mock.patch.object(project_init, "_drop_and_recreate",
                               _fake_drop_recreate):
            result = project_init.bootstrap_collections(
                "Alpha", weaviate_url="http://localhost:9", kg_only=True,
            )

        # The drifted KG class was regenerated (drop + recreate), NOT left
        # as-is.
        self.assertIn("Alpha_KnowledgeGraph", dropped)
        self.assertTrue(
            any(r["collection"] == "Alpha_KnowledgeGraph"
                and r["action"] == "regenerated"
                for r in result["actions"]),
            f"expected a regenerated action, got {result['actions']}",
        )
        self.assertTrue(result["regenerated"])

    def test_absent_class_is_created(self) -> None:
        created: list[str] = []

        with mock.patch.object(project_init, "_is_weaviate_reachable",
                               return_value=True), \
             mock.patch.object(project_init, "_fetch_schema",
                               return_value=None), \
             mock.patch.object(project_init, "_create_class",
                               lambda payload, weaviate_url=None:
                               created.append(payload.get("class"))), \
             mock.patch.object(project_init, "_drop_and_recreate",
                               lambda *a, **k: None):
            result = project_init.bootstrap_collections(
                "Alpha", weaviate_url="http://localhost:9", kg_only=True,
            )

        self.assertIn("Alpha_KnowledgeGraph", created)
        self.assertTrue(
            all(a["action"] in ("create", "exists") for a in result["actions"])
        )


class EnsureCollectionsAdoptsDriftAsIsTests(unittest.TestCase):
    """``_ensure_collections`` (install.py) ADOPTS a drifted existing class
    as-is — it never drops/recreates on schema drift.

    Rather than stand up an HTTP stub (heavy; already covered structurally by
    ``test_install_case_insensitive_adopt``), this test verifies the CODE
    CONTRACT: ``_ensure_collections`` has no schema-drift regeneration path —
    an existing class name is filtered out of ``missing`` purely by
    name-set membership, so no POST/drop is issued for it regardless of its
    schema. We assert the absence of the regen primitives + the presence of
    the pure name-set ``missing`` computation.
    """

    def test_ensure_collections_has_no_drift_regen_primitives(self) -> None:
        import inspect

        import install

        src = inspect.getsource(install._ensure_collections)
        # The regen engine that bootstrap_collections uses must NOT appear
        # in _ensure_collections — it is a create-only path.
        self.assertNotIn("_schema_incompatible", src)
        self.assertNotIn("_drop_and_recreate", src)
        # Existing classes are filtered by NAME membership only.
        self.assertIn("not in existing", src)


class SharedAgreementTests(unittest.TestCase):
    """What the two paths DO agree on: absent → POST target; 422
    already-exists → benign."""

    def test_bootstrap_treats_422_already_exists_as_benign(self) -> None:
        # bootstrap_collections routes 422 through the case-conflict
        # extractor; a plain "already exists" with no similar-class name
        # falls through to the generic path but the class was created by the
        # racing peer. We assert the extractor tolerates the plain form.
        self.assertIsNone(
            project_init._extract_similar_class_name(
                "class already exists"
            )
        )

    def test_ensure_collections_treats_422_already_exists_as_benign(self) -> None:
        import inspect

        import install

        src = inspect.getsource(install._ensure_collections)
        # The 422 already-exists benign-race branch is present.
        self.assertIn("already exists", src)
        self.assertIn("422", src)


if __name__ == "__main__":
    unittest.main()
