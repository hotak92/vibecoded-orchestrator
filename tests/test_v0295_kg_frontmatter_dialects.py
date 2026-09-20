"""v0.2.95 — sync_knowledge_graph.py parses BOTH KG frontmatter dialects.

Field defect this pins: nodes written in the NESTED dialect (Claude Code
skill/memory frontmatter contract — node keys tucked under ``metadata:``,
node named with ``name:`` instead of ``title:``) parsed as if they had no
metadata at all. Because the top level had no ``tags``/``type``, the inline
``#tag`` body harvest fired over the whole prose and returned issue/section
references as tags (``['4', '2', '1', '14', …]`` — NOT a string being
indexed character-wise), and the folder name became the node type
("concepts" instead of "concept"). 57 of 71 nodes in one project use the
nested dialect; shipped templates use ONLY the canonical top-level dialect,
which stays canonical (top level wins when both are present).

All tests are pure parsing — no Weaviate, no Ollama, no network.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"

_MOD = None


def _load_sync_module():
    """Import the script under a unique module name (test_v0270 pattern).

    Import happens lazily, i.e. under the suite's autouse hermeticity
    fixtures (VCT_DISABLE_HUB_RESOLVER / unroutable WEAVIATE_URL sentinel).
    Only the pure parse path is exercised — the module never connects to a
    backend at import time.
    """
    global _MOD
    if _MOD is None:
        mod_name = f"_sync_kg_fm_dialects_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(mod_name, SCRIPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        _MOD = mod
    return _MOD


@pytest.fixture
def sync_mod(tmp_path, monkeypatch):
    """Loaded module with KNOWLEDGE_ROOT pointed at the tmp 'project'.

    parse_markdown_node's folder-name type fallback does
    ``file_path.relative_to(KNOWLEDGE_ROOT)``; pointing the module global
    at the tmp tree (it is re-read at call time) lets real files live in
    knowledge/<folder>/ without touching the repo.
    """
    mod = _load_sync_module()
    monkeypatch.setattr(mod, "KNOWLEDGE_ROOT", tmp_path / "knowledge")
    return mod


def _write_node(tmp_path: Path, name: str, text: str, folder: str = "concepts") -> Path:
    node_dir = tmp_path / "knowledge" / folder
    node_dir.mkdir(parents=True, exist_ok=True)
    path = node_dir / name
    path.write_text(text, encoding="utf-8")
    return path


ARTUP_SHAPE = """---
name: artup-pay-payment-orchestration
description: Acme PAY — payment orchestration module 6.3
metadata:
  type: concept
  tags: [Acme, Acme-PAY, payments]
---

See issues #4 and #12 (also section #14) for the incident history.
"""


class TestNestedDialect:
    def test_artup_shape_regression(self, sync_mod, tmp_path):
        """The exact field-reported shape parses with its real tags/type/title."""
        path = _write_node(tmp_path, "artup-pay.md", ARTUP_SHAPE)
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["tags"] == ["Acme", "Acme-PAY", "payments"]
        assert node["node_type"] == "concept"
        assert node["title"] == "artup-pay-payment-orchestration"

    def test_promoted_scope_reaches_consumers(self, sync_mod, tmp_path):
        """A nested metadata.scope is promoted — the archive/scope sniff
        (`_node_scope(fm or {})`) sees the same one shape."""
        path = _write_node(
            tmp_path,
            "scoped.md",
            "---\nname: scoped\nmetadata:\n  type: concept\n  scope: shared\n---\nbody\n",
        )
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["scope"] == "shared"

    def test_promotion_note_names_the_file(self, sync_mod, tmp_path, capsys):
        path = _write_node(tmp_path, "noted.md", ARTUP_SHAPE)
        sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        out = capsys.readouterr().out
        assert "promoted nested `metadata:`" in out
        assert "noted.md" in out

    def test_name_slots_above_h1_but_below_title(self, sync_mod, tmp_path):
        # name (declared frontmatter) beats the body-derived H1…
        path = _write_node(
            tmp_path,
            "with_h1.md",
            "---\nname: my-node\nmetadata:\n  type: concept\n---\n"
            "# A Directive Heading\nbody\n",
        )
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["title"] == "my-node"
        # …but an explicit title: still wins over name:.
        path = _write_node(
            tmp_path,
            "titled.md",
            "---\ntitle: The Title\nname: other-name\nmetadata:\n  type: concept\n---\nbody\n",
        )
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["title"] == "The Title"


class TestTopLevelDialect:
    def test_canonical_frontmatter_unchanged(self, sync_mod, tmp_path):
        text = (
            "---\n"
            "title: Canonical Node\n"
            "type: tool\n"
            "tags: [python, workflow, tooling]\n"
            "created: 2026-01-15T10:30:00Z\n"
            "status: active\n"
            "---\n"
            "Body mentioning #4 and #12 in prose.\n"
        )
        path = _write_node(tmp_path, "canonical.md", text)
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["title"] == "Canonical Node"
        assert node["node_type"] == "tool"
        assert node["tags"] == ["python", "workflow", "tooling"]

    def test_top_level_wins_over_nested(self, sync_mod, tmp_path):
        text = (
            "---\n"
            "title: Top Wins\n"
            "type: tool\n"
            "tags: [alpha]\n"
            "metadata:\n"
            "  type: concept\n"
            "  tags: [beta, gamma]\n"
            "---\nbody\n"
        )
        path = _write_node(tmp_path, "both.md", text)
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["title"] == "Top Wins"
        assert node["node_type"] == "tool"
        assert node["tags"] == ["alpha"]

    def test_folder_fallback_type_unchanged(self, sync_mod, tmp_path):
        path = _write_node(tmp_path, "nofm.md", "# Just a heading\n\n#payments body\n")
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["node_type"] == "concepts"  # folder name, pre-existing rule


class TestTagsStringForms:
    def test_string_tags_split_on_commas_and_whitespace(self, sync_mod, tmp_path):
        path = _write_node(
            tmp_path,
            "strtags.md",
            "---\ntype: concept\ntags: \"a, b c\"\n---\nbody\n",
        )
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["tags"] == ["a", "b", "c"]

    def test_hash_prefixed_string_tokens_lose_the_hash(self, sync_mod, tmp_path):
        path = _write_node(
            tmp_path,
            "hashtok.md",
            "---\ntype: concept\ntags: \"#x, y\"\n---\nbody\n",
        )
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["tags"] == ["x", "y"]

    def test_empty_string_tags_yield_empty_list(self, sync_mod, tmp_path):
        path = _write_node(
            tmp_path, "empty.md", "---\ntype: concept\ntags: \"\"\n---\nbody\n"
        )
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["tags"] == []

    def test_split_tags_string_unit(self, sync_mod):
        assert sync_mod._split_tags_string("a, b c") == ["a", "b", "c"]
        assert sync_mod._split_tags_string("") == []
        assert sync_mod._split_tags_string("   ") == []
        assert sync_mod._split_tags_string("#x,y") == ["x", "y"]


class TestHarvestSuppression:
    def test_no_numeric_tags_when_frontmatter_exists(self, sync_mod, tmp_path):
        """Frontmatter block without a tags key → NO body harvest at all
        (prose `#4 #12 #14` must not become tags)."""
        text = (
            "---\nname: harvest-suppressed\nmetadata:\n  type: concept\n---\n"
            "See issues #4 and #12 (also section #14).\n"
        )
        path = _write_node(tmp_path, "suppressed.md", text)
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["tags"] == []

    def test_malformed_frontmatter_block_still_suppresses_harvest(
        self, sync_mod, tmp_path
    ):
        # A block that exists but fails YAML parsing declares frontmatter
        # (parse_frontmatter returns {} for it) — harvest must not fire.
        text = "---\ntitle: [unclosed\n---\nprose with #4 and #12\n"
        path = _write_node(tmp_path, "malformed.md", text)
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["tags"] == []

    def test_harvest_without_frontmatter_drops_numeric_tokens(
        self, sync_mod, tmp_path
    ):
        path = _write_node(
            tmp_path, "noheading.md", "Body text with #payments and #4 and #12 refs.\n"
        )
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["tags"] == ["payments"]

    def test_no_tags_anywhere_keeps_warning_path(self, sync_mod, tmp_path):
        """Frontmatter present, no tags declared, no body tags → tags [] and
        the pre-existing 'Too few tags' validation warning still fires."""
        path = _write_node(
            tmp_path, "notags.md", "---\nname: no-tags\nmetadata:\n  type: concept\n---\nbody\n"
        )
        node = sync_mod.parse_markdown_node(path.read_text(encoding="utf-8"), path)
        assert node["tags"] == []
        warnings = sync_mod.validate_node_against_vocabulary(node, path)
        assert any("Too few tags" in w for w in warnings)


class TestParseFrontmatterContract:
    def test_no_block_returns_none(self, sync_mod):
        fm, body = sync_mod.parse_frontmatter("plain text\n")
        assert fm is None
        assert body == "plain text\n"

    def test_empty_block_returns_empty_mapping(self, sync_mod):
        fm, body = sync_mod.parse_frontmatter("---\n---\nbody here\n")
        assert fm == {}
        assert body == "body here"

    def test_malformed_block_returns_empty_mapping(self, sync_mod):
        fm, _body = sync_mod.parse_frontmatter("---\n: [bad\n---\nbody\n")
        assert fm == {}

    def test_nested_promotion_in_parse_frontmatter(self, sync_mod):
        fm, _body = sync_mod.parse_frontmatter(
            "---\nname: n1\nmetadata:\n  type: concept\n  tags: [a, b]\n---\nx\n"
        )
        assert fm["type"] == "concept"
        assert fm["tags"] == ["a", "b"]
        assert fm["title"] == "n1"
