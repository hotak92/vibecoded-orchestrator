# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 task #33 — SSOT tests for the OPEN KG vocabulary parser.

``vco_lib/kg_vocabulary.py`` turns a project's ``knowledge/VOCABULARY.md``
into the open node-type set, the open trusted-subfolder registry, and the
open node_type → folder mapping.

Fixture discipline (source-text-gates lesson): every fixture is built FROM
the REAL shipped template's text — the authoritative producer's format —
never from this parser's own convention. The custom-declaration fixture is
the template's OWN documented example, extracted verbatim from the
"Declaring your own node types" fenced block and re-planted outside a
fence. A meta-test first proves the naive ``alias:`` scan (the pre-#33
seed) IS fooled by that fenced example, so the anti-fooling assertions
demonstrably exercise the hazard.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import kg_vocabulary as kv  # noqa: E402

TEMPLATE_PATH = REPO_ROOT / "templates" / "knowledge" / "VOCABULARY.md"
REAL_TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")

#: The template's relationship-section names — realistic non-class headings
#: (``#### **`uses`** (co:uses)`` shape) that must never become node types.
RELATIONSHIP_NAMES = {
    "uses", "implements", "extends", "buildson",
    "relatedto", "partof", "dependson", "sameas",
}


def _documented_custom_example() -> str:
    """Extract the custom-declaration example from the template's OWN
    fenced ```markdown block (the authoritative producer of the format).

    Failing to find it means the documentation section drifted — loud
    failure, not a silently self-invented fixture.
    """
    m = re.search(r"```markdown\n(.*?)```", REAL_TEMPLATE, re.DOTALL)
    assert m, (
        "templates/knowledge/VOCABULARY.md no longer carries the fenced "
        "```markdown declaration example — fixture source drifted"
    )
    example = m.group(1)
    assert "co:Thought" in example and "**Folder**" in example, (
        "the fenced example no longer shows a custom class with a Folder "
        "line — fixture source drifted"
    )
    return example


#: Real template + its own documented example planted OUTSIDE a fence:
#: a project that declared type `thought` with folder `thoughts`.
FIXTURE_WITH_CUSTOM = REAL_TEMPLATE + "\n" + _documented_custom_example() + "\n"


# ── Producer-format liveness + red-proof of the fooling hazard ───────────────

def test_template_still_carries_the_declaration_shape() -> None:
    """Axis liveness: the shipped template still declares its nine classes
    with the heading shape this parser is built for.

    Independent oracle: plain string structure, deliberately NOT the
    parser's regex (never audit an instrument with itself).
    """
    lines = set(REAL_TEMPLATE.splitlines())
    for co_name in (
        "Project", "Concept", "Tool", "Model", "Hardware",
        "Research", "Pattern", "Insight", "Guide",
    ):
        expected = f"#### **`co:{co_name}`** (alias: `{co_name.lower()}`)"
        assert expected in lines, (
            f"missing built-in class heading {expected!r} — template format "
            f"drifted; realign vco_lib/kg_vocabulary.py's parser"
        )


def test_naive_alias_scan_is_fooled_by_the_docs_example() -> None:
    """Meta-test (red-proof): the pre-#33 seed regex — ``alias:`` anywhere —
    DOES absorb the fenced documentation example, proving the fixture
    exercises the fooling hazard the structured parser must resist."""
    naive = {m.lower() for m in re.findall(r"alias:\s*`([A-Za-z0-9_-]+)`", REAL_TEMPLATE)}
    assert "thought" in naive, (
        "the naive scan is no longer fooled by the template — the docs "
        "example changed; keep a fenced (alias: `...`) example so the "
        "anti-fooling tests stay meaningful"
    )


def test_real_template_parses_to_exactly_the_builtins() -> None:
    """The shipped template (docs section included) contributes NOTHING
    beyond the nine built-ins — fenced examples and prose are inert."""
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE)
    assert parsed.node_types == kv.BUILTIN_NODE_TYPES
    assert parsed.knowledge_subfolders == kv.BUILTIN_KNOWLEDGE_SUBFOLDERS
    assert dict(parsed.node_type_to_folder) == dict(kv.BUILTIN_NODE_TYPE_TO_FOLDER)
    assert parsed.warnings == ()


# ── Open-vocabulary declarations ─────────────────────────────────────────────

def test_custom_declaration_extends_types_folders_and_routing() -> None:
    parsed = kv.parse_vocabulary_text(FIXTURE_WITH_CUSTOM)
    assert "thought" in parsed.node_types
    assert "thoughts" in parsed.knowledge_subfolders
    assert parsed.folder_for("thought") == "thoughts"
    # Built-ins untouched alongside the custom declaration.
    assert kv.BUILTIN_NODE_TYPES <= parsed.node_types
    assert kv.BUILTIN_KNOWLEDGE_SUBFOLDERS <= parsed.knowledge_subfolders
    assert parsed.folder_for("project") == "projects"


def test_custom_type_without_folder_line_defaults_to_concepts() -> None:
    heading = "#### **`co:Riff`** (alias: `riff`)\n- **Definition**: An improvised exploration\n"
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE + "\n" + heading)
    assert "riff" in parsed.node_types
    assert parsed.folder_for("riff") == kv.DEFAULT_NODE_FOLDER
    # No folder registered — only explicit Folder lines extend the registry.
    assert parsed.knowledge_subfolders == kv.BUILTIN_KNOWLEDGE_SUBFOLDERS


def test_alias_is_lowercased() -> None:
    heading = "#### **`co:Thought`** (alias: `THOUGHT`)\n"
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE + "\n" + heading)
    assert "thought" in parsed.node_types
    assert "THOUGHT" not in parsed.node_types


# ── Anti-fooling: descriptions of declarations are not declarations ──────────

def test_relationship_sections_never_absorbed_as_types() -> None:
    """The REAL relationship sections (``#### **`uses`** (co:uses)``) ride
    inside both fixtures — none of them may become a node type."""
    for text in (REAL_TEMPLATE, FIXTURE_WITH_CUSTOM):
        assert "#### **`uses`** (co:uses)" in text  # fixture really has them
        parsed = kv.parse_vocabulary_text(text)
        assert not (RELATIONSHIP_NAMES & {t.lower() for t in parsed.node_types})


def test_prose_mention_of_alias_not_absorbed() -> None:
    """The sync validator's own guidance text names the declaration shape —
    a VOCABULARY.md quoting it in prose must not gain a type."""
    prose = (
        "\nDeclare custom types in knowledge/VOCABULARY.md as a class "
        "heading with (alias: `daydream`)\n"
    )
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE + prose)
    assert "daydream" not in parsed.node_types


def test_folder_line_outside_a_class_section_is_ignored() -> None:
    stray = "\n### Some notes\n- **Folder**: `stray`\n"
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE + stray)
    assert "stray" not in parsed.knowledge_subfolders


def test_tilde_fence_also_inert() -> None:
    fenced = "\n~~~\n#### **`co:Ghost`** (alias: `ghost`)\n~~~\n"
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE + fenced)
    assert "ghost" not in parsed.node_types


# ── Built-in preservation ────────────────────────────────────────────────────

def test_builtin_reroute_refused_with_warning() -> None:
    reroute = (
        "\n#### **`co:Concept`** (alias: `concept`)\n"
        "- **Folder**: `elsewhere`\n"
    )
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE + reroute)
    assert parsed.folder_for("concept") == "concepts"  # built-in routing wins
    # The explicitly-named folder is still trusted for paths…
    assert "elsewhere" in parsed.knowledge_subfolders
    # …and the refusal is surfaced.
    assert any("concept" in w and "elsewhere" in w for w in parsed.warnings)


def test_unmapped_builtin_reroute_also_refused() -> None:
    reroute = (
        "\n#### **`co:Insight`** (alias: `insight`)\n"
        "- **Folder**: `wisdom`\n"
    )
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE + reroute)
    assert parsed.folder_for("insight") == kv.DEFAULT_NODE_FOLDER
    assert any("insight" in w for w in parsed.warnings)


# ── Robust loading ───────────────────────────────────────────────────────────

def _project(tmp_path: Path, text: str | None) -> Path:
    root = tmp_path / "proj"
    (root / "knowledge").mkdir(parents=True)
    if text is not None:
        (root / "knowledge" / "VOCABULARY.md").write_text(text, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _fresh_cache():
    kv.clear_vocabulary_cache()
    yield
    kv.clear_vocabulary_cache()


def test_missing_file_builtins_only_no_warning(tmp_path: Path) -> None:
    vocab = kv.load_vocabulary(_project(tmp_path, None))
    assert vocab.node_types == kv.BUILTIN_NODE_TYPES
    assert vocab.warnings == ()


def test_unicode_decode_error_builtins_only_no_crash(tmp_path: Path) -> None:
    root = _project(tmp_path, None)
    (root / "knowledge" / "VOCABULARY.md").write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    vocab = kv.load_vocabulary(root)
    assert vocab.node_types == kv.BUILTIN_NODE_TYPES
    assert vocab.knowledge_subfolders == kv.BUILTIN_KNOWLEDGE_SUBFOLDERS
    assert any("unreadable" in w for w in vocab.warnings)


def test_oserror_builtins_only_no_crash(tmp_path: Path) -> None:
    root = _project(tmp_path, None)
    (root / "knowledge" / "VOCABULARY.md").mkdir()  # a directory → IsADirectoryError
    vocab = kv.load_vocabulary(root)
    assert vocab.node_types == kv.BUILTIN_NODE_TYPES
    assert any("unreadable" in w for w in vocab.warnings)


def _bump_mtime(path: Path, delta_ns: int = 1_000_000_000) -> None:
    """Guarantee a changed mtime_ns even on coarse-timestamp filesystems."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


def test_mid_session_edit_picked_up_without_clear(tmp_path: Path) -> None:
    """M3 driving use case: declare a type mid-session, the very next load
    sees it — no clear_vocabulary_cache(), no process restart."""
    root = _project(tmp_path, REAL_TEMPLATE)
    assert "thought" not in kv.load_vocabulary(root).node_types

    vocab_file = root / "knowledge" / "VOCABULARY.md"
    vocab_file.write_text(FIXTURE_WITH_CUSTOM, encoding="utf-8")
    _bump_mtime(vocab_file)

    assert "thought" in kv.load_vocabulary(root).node_types


def test_missing_then_created_transition(tmp_path: Path) -> None:
    """A missing file caches under the sentinel token, and the per-call
    stat picks up the file the moment it exists."""
    root = _project(tmp_path, None)
    assert kv.load_vocabulary(root).node_types == kv.BUILTIN_NODE_TYPES
    # Second missing-file load: sentinel cache hit, still built-ins.
    assert kv.load_vocabulary(root).node_types == kv.BUILTIN_NODE_TYPES

    (root / "knowledge" / "VOCABULARY.md").write_text(
        FIXTURE_WITH_CUSTOM, encoding="utf-8"
    )
    assert "thought" in kv.load_vocabulary(root).node_types


def test_use_cache_false_bypasses_same_mtime_content_change(tmp_path: Path) -> None:
    """The cache is keyed by mtime_ns, not content — a rewrite with the
    mtime deliberately reset is invisible to cached reads (proving the
    keying) and ``use_cache=False`` remains the hatch that re-reads AND
    refreshes the entry."""
    root = _project(tmp_path, REAL_TEMPLATE)
    vocab_file = root / "knowledge" / "VOCABULARY.md"
    original_ns = vocab_file.stat().st_mtime_ns
    assert "thought" not in kv.load_vocabulary(root).node_types

    vocab_file.write_text(FIXTURE_WITH_CUSTOM, encoding="utf-8")
    st = vocab_file.stat()
    os.utime(vocab_file, ns=(st.st_atime_ns, original_ns))  # same token

    # Cached read: token unchanged → old parse served.
    assert "thought" not in kv.load_vocabulary(root).node_types
    # Bypass re-reads and refreshes the entry…
    assert "thought" in kv.load_vocabulary(root, use_cache=False).node_types
    # …so the next cached read (token still unchanged) sees the new value.
    assert "thought" in kv.load_vocabulary(root).node_types


def test_capacity_soft_warning_past_256(tmp_path: Path) -> None:
    many = "\n".join(
        f"#### **`co:T{i}`** (alias: `t{i}`)" for i in range(260)
    )
    parsed = kv.parse_vocabulary_text(REAL_TEMPLATE + "\n" + many)
    assert len(parsed.node_types) > kv.RL_TYPE_CAPACITY_SOFT_CAP
    assert any(str(kv.RL_TYPE_CAPACITY_SOFT_CAP) in w for w in parsed.warnings)
