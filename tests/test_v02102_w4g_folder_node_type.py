# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W4-G item 5 — a folder-derived ``node_type`` is the SINGULAR type.

Dogfood finding: a node without a frontmatter ``type:`` under
``knowledge/concepts/`` was stored as ``node_type: "concepts"`` (the folder
name) — outside the singular vocabulary filters and the RL type registry
use. The sync now maps the folder through the one vocabulary home
(``kg_vocabulary.node_type_for_folder``), and rows already stored under the
plural heal on the zero-embed metadata repair (``KG_METADATA_REPAIR_BUMPS``
gains 0.2.102, so every project's next drift probe reports the pass owed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import kg_vocabulary as kv  # noqa: E402
from test_v0295_kg_metadata_repair_on_skip import (  # noqa: E402
    _CountingServer,
    _load_sync_module,
    _RepairTestBase,
)


@pytest.mark.parametrize("folder,want", [
    ("concepts", "concept"), ("tools", "tool"), ("projects", "project"),
    ("models", "model"), ("patterns", "pattern"), ("insights", "insight"),
    ("research", "research"), ("hardware", "hardware"),
    ("Concepts", "concept"),
    ("notes", "notes"), ("techniques", "techniques"),  # no such type: unchanged
])
def test_builtin_folder_table(folder, want):
    assert kv.node_type_for_folder(folder, kv.builtin_vocabulary()) == want


def test_custom_folder_line_maps_to_its_type():
    vocab = kv.parse_vocabulary_text(
        "#### **`co:Thought`** (alias: `thought`)\n- **Folder**: `thoughts`\n"
    )
    assert kv.node_type_for_folder("thoughts", vocab) == "thought"


TYPELESS_NODE = "---\ntitle: Plain Node\ntags: [alpha]\n---\n\nA body without a type.\n"


class FolderTypeHealsWithoutReembed(_RepairTestBase):
    def _typeless_path(self) -> Path:
        path = self.root / "knowledge" / "concepts" / "plain-node.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TYPELESS_NODE, encoding="utf-8")
        return path

    def test_parse_stores_the_singular_type(self):
        mod = _load_sync_module(self.root)
        path = self._typeless_path()
        node = mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["node_type"] == "concept"
        assert node["type_from_frontmatter"] is False

    def test_plural_row_heals_on_the_embed_skip_path(self):
        """A row stored by the pre-fix parse ("concepts") is patched to
        "concept" — content hash untouched, zero embeds."""
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._typeless_path()
        self._seed_rows(mod, server, path, {
            "title": "Plain Node", "node_type": "concepts",
            "tags": ["alpha"], "external_links": "",
        })
        outcome, out = self._sync(mod, server, path)
        assert outcome.status == mod.OUTCOME_EMBED_SKIPPED
        assert server.embed_calls == 0
        assert server.opts.inserts == 0 and server.opts.deletes == 0
        assert self._stored_props(server, "row-1")["node_type"] == "concept"
        assert "node_type" in out and "Metadata repair" in out

    def test_singular_row_is_left_alone(self):
        mod = _load_sync_module(self.root)
        server = _CountingServer()
        path = self._typeless_path()
        self._seed_rows(mod, server, path, {
            "title": "Plain Node", "node_type": "concept",
            "tags": ["alpha"], "external_links": "",
        })
        self._sync(mod, server, path)
        assert server.opts.updates == [] and server.embed_calls == 0


def test_repair_ladder_owes_a_pass_to_0_2_95_stamps():
    from vco_lib import install_weaviate as iw

    assert iw.KG_METADATA_REPAIR_BUMPS[-1] == "0.2.102"
    assert iw.kg_metadata_repair_due("0.2.95") is True
    assert iw.kg_metadata_repair_due("0.2.102") is False
