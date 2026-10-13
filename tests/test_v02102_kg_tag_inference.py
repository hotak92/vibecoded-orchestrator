# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-X — stored KG tags are EXACTLY the node's frontmatter (OD-1).

Owner decision OD-1 (2026-10-07, re-confirmed 2026-10-13): "tags = exactly
the node's frontmatter. The KG is NOT hierarchical → tags must NOT propagate
through links at all (remove link-based tag inference; owner-approved
removal)". The pre-v0.2.102 ``infer_tags_from_typed_links`` copied a link
target's tags onto the linking node, and because it read the target's STORED
tags (authored ∪ inferred), every sync closed one more hop: collections
synced by those versions hold snowballed tag sets (60-90 stored tags on a
node whose frontmatter declares 7). An interim W4-C rewrite kept a one-hop
variant admitted through ``knowledge/TAG_HIERARCHY.md``; that is the rejected
option and is gone too.

These tests pin:

* NO propagation: a node linking (``uses`` / ``implements`` / ``extends`` /
  ``buildsOn``) to a heavily-tagged node stores exactly its own frontmatter
  tags, even when the target's tags are declared in ``TAG_HIERARCHY.md``;
* CONVERGENCE: a row stored with an inflated tag set and unchanged text is
  rewritten to the frontmatter by a property-only patch on the embed-skip
  path (no re-embed), and the repair generation ladder owes that whole-tree
  pass to every install stamped below 0.2.102 — including installs that skip
  versions.

Pure in-memory fakes (the shared ``test_v0270`` harness) — no Weaviate, no
Ollama.
"""

from __future__ import annotations

import pytest

from tests.test_v0270_kg_sync_batch_title_collision import (
    _FakeServer,
    _load_sync_module,
)
from vco_lib import kg_metadata_repair

#: A hierarchy that DECLARES every tag the heavily-tagged target carries, so
#: any surviving TAG_HIERARCHY-admitted inference would fire.
TAG_HIERARCHY = (
    "# Knowledge Graph Tag Hierarchy\n\n## Tag Hierarchy\n\n```\n"
    "#AI (Artificial Intelligence)\n"
    "├── #vector-db (Vector databases)\n"
    "├── #embeddings\n"
    "└── #testing\n"
    "#workflow\n"
    "└── #automation\n"
    "```\n"
)

HEAVY_TAGS = ["vector-db", "embeddings", "testing", "workflow", "automation",
              "AI", "database", "search"]

LINK_RELATIONS = ("uses", "implements", "extends", "buildsOn", "relatedTo")


def _write_node(root, rel, title, tags=None, body="Body."):
    """A knowledge node whose frontmatter carries *tags*."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_list = ", ".join(tags or [])
    path.write_text(
        f"---\ntitle: {title}\ntype: concept\nstatus: active\n"
        f"tags: [{tag_list}]\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """Fresh sync module bound to a tmp project root + the tokenized Filter."""
    for key in ("KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                "DUAL_EMBEDDING_ENABLED", "VCT_DISABLE_HUB_RESOLVER"):
        monkeypatch.setenv(key, "placeholder")
    from tests.test_v02101_kg_exact_path_delete import _TokFilter

    mod = _load_sync_module(tmp_path)
    setattr(mod, "Filter", _TokFilter)
    server = _FakeServer()
    # The metadata-repair patch path calls ``data.update(uuid=..., properties=...)``
    # (vco_lib.kg_metadata_repair's writer seam) — the shared v0.2.70 fake
    # predates it. Teach it here, class-level via monkeypatch so the teardown
    # restores the untouched class for the rest of the session. Every call is
    # recorded so a test can prove the convergence was a PATCH.
    from tests.test_v0270_kg_sync_batch_title_collision import _FakeData

    updates: list = []

    def _fake_update(self, uuid=None, properties=None):  # noqa: A002
        updates.append((str(uuid), dict(properties or {})))
        row = self._store.get(str(uuid))
        if row is not None:
            row.properties.update(properties or {})

    monkeypatch.setattr(_FakeData, "update", _fake_update, raising=False)
    (tmp_path / "knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "knowledge" / "TAG_HIERARCHY.md").write_text(
        TAG_HIERARCHY, encoding="utf-8",
    )
    return mod, server, tmp_path, updates


def _rows(mod, server, rel) -> list:
    from vco_lib.paths import to_posix_rel

    canonical = to_posix_rel(rel)
    store = server.client.collections.get(mod.COLLECTION_NAME)._store
    return [o for o in store.values() if o.properties.get("file_path") == canonical]


def _synced_tags(mod, server, rel) -> list:
    """The stored tags on every stored row of *rel* (one node's rows share them)."""
    rows = _rows(mod, server, rel)
    tags = {tuple(sorted(o.properties.get("tags") or ())) for o in rows}
    assert len(tags) == 1, f"no single stored tag set for {rel}: {tags}"
    return sorted(tags.pop())


# ─── No propagation through links, of any kind ───────────────────────────


@pytest.mark.parametrize("relation", LINK_RELATIONS)
def test_linker_stores_exactly_its_frontmatter_tags(sync_env, relation):
    """A node linking to a heavily-tagged node keeps exactly its own tags —
    even though every target tag is declared in TAG_HIERARCHY.md (the
    W4-C admission gate that is now removed)."""
    mod, server, root, _updates = sync_env
    _write_node(root, "knowledge/concepts/heavy.md", "Heavy Node", tags=HEAVY_TAGS)
    _write_node(root, "knowledge/concepts/linker.md", "Linker Node",
                tags=["python"],
                body=f"Linker [[{relation}::Heavy Node]].")
    for _pass in (1, 2):
        for rel in ("knowledge/concepts/heavy.md", "knowledge/concepts/linker.md"):
            assert mod.sync_node(server, root / rel)

    assert _synced_tags(mod, server, "knowledge/concepts/linker.md") == ["python"]
    assert _synced_tags(mod, server, "knowledge/concepts/heavy.md") == sorted(HEAVY_TAGS)


def test_linker_without_tags_stores_none(sync_env):
    """A frontmatter with an empty tag list stores an empty list — the link
    target contributes nothing, so there is no "inherit when empty" arm."""
    mod, server, root, _updates = sync_env
    _write_node(root, "knowledge/concepts/heavy.md", "Heavy Node", tags=HEAVY_TAGS)
    _write_node(root, "knowledge/concepts/bare.md", "Bare Node", tags=[],
                body="Bare [[uses::Heavy Node]] and [[extends::Heavy Node]].")
    assert mod.sync_node(server, root / "knowledge/concepts/heavy.md")
    assert mod.sync_node(server, root / "knowledge/concepts/bare.md")
    assert _synced_tags(mod, server, "knowledge/concepts/bare.md") == []


def test_chain_never_propagates(sync_env):
    """A→B→C over two full passes: nobody acquires anybody else's tags."""
    mod, server, root, _updates = sync_env
    _write_node(root, "knowledge/concepts/a.md", "Node A", tags=["AI"],
                body="A uses [[uses::Node B]].")
    _write_node(root, "knowledge/concepts/b.md", "Node B", tags=["testing"],
                body="B uses [[buildsOn::Node C]].")
    _write_node(root, "knowledge/concepts/c.md", "Node C", tags=HEAVY_TAGS)
    for _pass in (1, 2):
        for rel in ("knowledge/concepts/a.md", "knowledge/concepts/b.md",
                    "knowledge/concepts/c.md"):
            assert mod.sync_node(server, root / rel)
    assert _synced_tags(mod, server, "knowledge/concepts/a.md") == ["AI"]
    assert _synced_tags(mod, server, "knowledge/concepts/b.md") == ["testing"]


def test_inference_entry_points_are_gone(sync_env):
    """The removed helpers must not linger as uncalled code a later edit
    could re-wire (owner-approved removal, OD-1)."""
    mod, _server, _root, _updates = sync_env
    for name in ("infer_tags_from_typed_links", "_declared_tag_hierarchy",
                 "_authored_tags_index", "_reset_tag_inference_caches"):
        assert not hasattr(mod, name), name


# ─── Convergence: inflated stored tags are rewritten, without a re-embed ──


def test_inflated_stored_tags_converge_by_property_patch(sync_env):
    """A row synced by an older version holds a snowballed tag set; its text
    is unchanged, so the sync takes the embed-skip path — and the metadata
    repair rewrites ``tags`` to the frontmatter with a property-only patch."""
    mod, server, root, updates = sync_env
    assert "tags" in kg_metadata_repair.REPAIRABLE_PROPERTIES

    own = ["AI", "python", "testing", "workflow", "rag", "mcp", "embeddings"]
    _write_node(root, "knowledge/concepts/heavy.md", "Heavy Node", tags=HEAVY_TAGS)
    _write_node(root, "knowledge/concepts/a.md", "Node A", tags=own,
                body="A uses [[uses::Heavy Node]].")
    for rel in ("knowledge/concepts/heavy.md", "knowledge/concepts/a.md"):
        assert mod.sync_node(server, root / rel)
    assert _synced_tags(mod, server, "knowledge/concepts/a.md") == sorted(own)

    # Simulate the pre-v0.2.102 stored state: 7 authored tags inflated to 75.
    inflated = own + HEAVY_TAGS + [f"snowball-tag-{i}" for i in range(60)]
    rows = _rows(mod, server, "knowledge/concepts/a.md")
    assert rows
    row_ids = sorted(str(o.uuid) for o in rows)
    for obj in rows:
        obj.properties["tags"] = list(inflated)
    updates.clear()

    outcome = mod.sync_node(server, root / "knowledge/concepts/a.md")
    # Embed-skip = no delete, no re-embed, no insert.
    assert outcome.status == mod.OUTCOME_EMBED_SKIPPED, outcome.reason
    assert _synced_tags(mod, server, "knowledge/concepts/a.md") == sorted(own)
    # The convergence was a property-only patch, on the SAME rows.
    assert updates, "no property patch was issued"
    assert all(set(payload) <= set(kg_metadata_repair.REPAIRABLE_PROPERTIES)
               for _uuid, payload in updates)
    assert sorted(str(o.uuid) for o in _rows(
        mod, server, "knowledge/concepts/a.md")) == row_ids

    # Converged: the next sync writes nothing.
    updates.clear()
    outcome = mod.sync_node(server, root / "knowledge/concepts/a.md")
    assert outcome.status == mod.OUTCOME_EMBED_SKIPPED
    assert updates == []


def test_ladder_owes_the_pass_to_every_older_or_skipping_install():
    """The whole-tree repair pass (which visits every node, including the
    unchanged ones) is owed to any install whose stamp predates 0.2.102 —
    the root's ``app_state`` leg and every project's stamp alike, and a
    version-skipping install (stamped 0.2.95, or never stamped) included."""
    from vco_lib import install_weaviate as iw

    assert "0.2.102" in iw.KG_METADATA_REPAIR_BUMPS
    for stale in (None, "0.2.95", "0.2.101"):
        assert iw.kg_metadata_repair_due(stale) is True, stale
    assert iw.kg_metadata_repair_due(iw.KG_METADATA_REPAIR_STAMP) is False


def test_project_stamp_below_0_2_102_reads_as_owed(tmp_path):
    """Per-project half: the stamp ``--check-drift`` reads."""
    from vco_lib import kg_metadata_repair_state as st

    assert st.repair_owed(tmp_path) is True  # never stamped
    assert st.write_stamp(tmp_path, "0.2.95")
    assert st.repair_owed(tmp_path) is True  # skipped versions
    assert st.write_stamp(tmp_path)
    assert st.repair_owed(tmp_path) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
