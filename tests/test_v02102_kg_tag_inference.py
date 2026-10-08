# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-C L1 — KG tag inference per owner decision OD-1 (2026-10-07).

The pre-v0.2.102 ``infer_tags_from_typed_links`` read the link target's
STORED Weaviate tags and admitted any tag matching two shape rules
("contains '-'", "uppercase or <15 chars"). Because stored tags are
themselves authored ∪ INFERRED, every sync closed one more hop over the link
graph — a transitive closure masquerading as single-hop inference (B3 report
:150-162).

OD-1 pins the replacement, and these tests pin OD-1:

* the ONLY source is the target's ON-DISK authored frontmatter ``tags``;
* a tag is admitted only when ALSO declared in ``knowledge/TAG_HIERARCHY.md``;
* the two shape rules are gone;
* exactly ONE hop across syncs (A→B→C never gives A the tags of C);
* authored tags are never removed;
* the metadata-repair path (``vco_lib.kg_metadata_repair``) keeps ``tags``
  repairable so the next embed-skip sync rewrites every node's stored tags
  to the new rule with NO re-embed.

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

TAG_HIERARCHY = (
    "# Knowledge Graph Tag Hierarchy\n\n## Tag Hierarchy\n\n```\n"
    "#AI (Artificial Intelligence)\n"
    "├── #vector-db (Vector databases)\n"
    "└── #testing\n"
"#workflow\n"
"└── #automation\n"
"```\n"
)


def _write_node(root, rel, title, tags=None, body="Body."):
    """A knowledge node whose AUTHORED frontmatter carries *tags*."""
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
    # restores the untouched class for the rest of the session.
    from tests.test_v0270_kg_sync_batch_title_collision import _FakeData

    def _fake_update(self, uuid=None, properties=None):  # noqa: A002
        row = self._store.get(str(uuid))
        if row is not None:
            row.properties.update(properties or {})

    monkeypatch.setattr(_FakeData, "update", _fake_update, raising=False)
    (tmp_path / "knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "knowledge" / "TAG_HIERARCHY.md").write_text(
        TAG_HIERARCHY, encoding="utf-8",
    )
    return mod, server, tmp_path


def _synced_tags(mod, server, root, rel) -> list:
    """The stored tags on every stored row of *rel* (one node's rows share them)."""
    from vco_lib.paths import to_posix_rel

    canonical = to_posix_rel(rel)
    store = server.client.collections.get(mod.COLLECTION_NAME)._store
    tags = {
        tuple(sorted(o.properties.get("tags") or ()))
        for o in store.values()
        if o.properties.get("file_path") == canonical
    }
    assert len(tags) == 1, f"no single stored tag set for {canonical}: {tags}"
    return sorted(tags.pop())


# ─── Unit level: the OD-1 admission rule ─────────────────────────────────


def test_admits_only_authored_and_declared_tags(sync_env):
    """Target authored [vector-db, undeclared-tag] → only the declared one."""
    mod, _server, root = sync_env
    _write_node(root, "knowledge/concepts/weaviate.md", "Weaviate",
                tags=["vector-db", "undeclared-tag"])
    node = {"tags": [], "typed_links": [
        {"relation_type": "uses", "target_title": "weaviate"},
    ]}
    assert mod.infer_tags_from_typed_links(node) == ["vector-db"]


def test_no_hierarchy_declared_nothing_is_inferred(sync_env):
    """An absent TAG_HIERARCHY.md admits nothing — never "any tag"."""
    mod, _server, root = sync_env
    (root / "knowledge" / "TAG_HIERARCHY.md").unlink()
    _write_node(root, "knowledge/concepts/weaviate.md", "Weaviate",
                tags=["vector-db"])
    node = {"tags": [], "typed_links": [
        {"relation_type": "uses", "target_title": "weaviate"},
    ]}
    assert mod.infer_tags_from_typed_links(node) == []


def test_hierarchy_parser_reads_hash_tokens_not_headings(sync_env):
    mod, _server, root = sync_env
    declared = mod._declared_tag_hierarchy()
    assert "vector-db" in declared and "AI" in declared and "workflow" in declared
    # Markdown headings and prose words never declare a tag.
    assert "Knowledge" not in declared and "Hierarchy" not in declared


def test_only_tag_relations_propagate(sync_env):
    """`relatedTo` (an untyped relation) carries no tags — the gate is unchanged."""
    mod, _server, root = sync_env
    _write_node(root, "knowledge/concepts/weaviate.md", "Weaviate",
                tags=["vector-db"])
    node = {"tags": [], "typed_links": [
        {"relation_type": "relatedTo", "target_title": "Weaviate"},
    ]}
    assert mod.infer_tags_from_typed_links(node) == []


# ─── The closure regression: one hop, across syncs ───────────────────────


def test_two_syncs_do_not_close_a_second_hop(sync_env):
    """A→B→C with C authored `vector-db`: B carries it, A NEVER does —
    on the FIRST pass and still after a SECOND full pass. Pre-fix, the
    second pass read B's STORED (inferred) tags and gave A `vector-db`."""
    mod, server, root = sync_env
    _write_node(root, "knowledge/concepts/a.md", "Node A", tags=["AI"],
                body="A uses [[uses::Node B]].")
    _write_node(root, "knowledge/concepts/b.md", "Node B", tags=[],
                body="B uses [[uses::Node C]].")
    _write_node(root, "knowledge/concepts/c.md", "Node C",
                tags=["vector-db", "undeclared-tag"])

    for _pass in (1, 2):
        for rel in ("knowledge/concepts/a.md", "knowledge/concepts/b.md",
                    "knowledge/concepts/c.md"):
            assert mod.sync_node(server, root / rel)

    # One hop: B carries C's declared tag; the undeclared one never moves.
    assert _synced_tags(mod, server, root, "knowledge/concepts/b.md") == [
        "vector-db",
    ]
    # A keeps its authored tag only — no second hop, ever.
    assert _synced_tags(mod, server, root, "knowledge/concepts/a.md") == ["AI"]


def test_authored_tags_are_never_removed(sync_env):
    """Inference only ADDS: A's authored tags survive every sync untouched."""
    mod, server, root = sync_env
    _write_node(root, "knowledge/concepts/a.md", "Node A",
                tags=["AI", "workflow"],
                body="A uses [[uses::Node B]].")
    _write_node(root, "knowledge/concepts/b.md", "Node B", tags=["testing"])
    for _pass in (1, 2):
        assert mod.sync_node(server, root / "knowledge/concepts/a.md")
    assert _synced_tags(mod, server, root, "knowledge/concepts/a.md") == [
        "AI", "testing", "workflow",
    ]


# ─── The repair path: a rule change rewrites tags with NO re-embed ───────


def test_tags_stay_repairable_on_the_embed_skip_path(sync_env):
    """OD-1's companion promise: ``tags`` is a repairable property, so the
    next embed-skip sync rewrites every stored tag set the new rule produces
    (property-only patch, never a re-embed)."""
    mod, server, root = sync_env
    assert "tags" in kg_metadata_repair.REPAIRABLE_PROPERTIES

    _write_node(root, "knowledge/concepts/a.md", "Node A", tags=["AI"])
    assert mod.sync_node(server, root / "knowledge/concepts/a.md")
    assert _synced_tags(mod, server, root, "knowledge/concepts/a.md") == ["AI"]

    # Simulate a row stored under the OLD rule: tags the new rule would not
    # produce, on a row whose content_hash still matches (unchanged text).
    from vco_lib.paths import to_posix_rel

    canonical = to_posix_rel("knowledge/concepts/a.md")
    store = server.client.collections.get(mod.COLLECTION_NAME)._store
    patched = 0
    for obj in store.values():
        if obj.properties.get("file_path") == canonical:
            obj.properties["tags"] = ["AI", "windows-gotcha", "PORTS"]
            patched += 1
    assert patched, "no stored row found to stale-tag"

    outcome = mod.sync_node(server, root / "knowledge/concepts/a.md")
    assert outcome.status == mod.OUTCOME_EMBED_SKIPPED, outcome.reason
    # The repair stamped the stored tags back to the authored set — without
    # re-embedding (the skip outcome above is the no-re-embed proof).
    assert _synced_tags(mod, server, root, "knowledge/concepts/a.md") == ["AI"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
