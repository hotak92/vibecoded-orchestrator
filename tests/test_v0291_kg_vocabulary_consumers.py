# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 task #33 — consumer tests for the OPEN KG vocabulary.

Two consumers route through the SSOT (``vco_lib/kg_vocabulary.py``):

* ``claude_mcp_servers/weaviate_mcp/server.py::_normalize_kg_file_path`` —
  declared custom subfolders are trusted like built-ins, and a path
  already rooted under ``knowledge/`` is NEVER double-prefixed (the
  pre-#33 mangling bug: ``knowledge/thoughts/foo.md`` with ``thoughts``
  undeclared became ``knowledge/concepts/knowledge/thoughts/foo.md``).
* ``templates/scripts/sync_knowledge_graph.py`` — the node validator
  accepts declared custom types (A-leg delegation) and keeps an inline
  fallback parser for the version-skew ImportError branch.

This file also carries the PARITY PINS both consumers' comments point at:
the server's built-in literals and the sync template's fallback parser
must match the SSOT (must-match comments at each site).

Fixture discipline: fixtures derive from the REAL shipped
``templates/knowledge/VOCABULARY.md`` — its own text plus its own
documented custom-declaration example — never from the parsers'
assumptions (source-text-gates lesson).
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_MCP_ROOT = REPO_ROOT / "claude_mcp_servers"
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from vco_lib import kg_vocabulary as kv  # noqa: E402

TEMPLATE_PATH = REPO_ROOT / "templates" / "knowledge" / "VOCABULARY.md"
SYNC_SCRIPT_PATH = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
REAL_TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")


def _documented_custom_example() -> str:
    """The template's own fenced ```markdown declaration example (the
    authoritative producer of the custom-declaration format)."""
    m = re.search(r"```markdown\n(.*?)```", REAL_TEMPLATE, re.DOTALL)
    assert m, "VOCABULARY.md lost its fenced declaration example"
    return m.group(1)


#: A project vocabulary that declares type `thought` with folder `thoughts`,
#: built from the producer's real text + its real documented example.
FIXTURE_WITH_CUSTOM = REAL_TEMPLATE + "\n" + _documented_custom_example() + "\n"


def _write_project(tmp_path: Path, vocabulary_text: str | None) -> Path:
    root = tmp_path / "proj"
    (root / "knowledge").mkdir(parents=True)
    if vocabulary_text is not None:
        (root / "knowledge" / "VOCABULARY.md").write_text(
            vocabulary_text, encoding="utf-8"
        )
    return root


@pytest.fixture(autouse=True)
def _fresh_vocab_cache():
    kv.clear_vocabulary_cache()
    yield
    kv.clear_vocabulary_cache()


# ═════════════════════════════════════════════════════════════════════════════
# Consumer 1 — weaviate_mcp/server.py
# ═════════════════════════════════════════════════════════════════════════════

def _load_server():
    try:
        from weaviate_mcp import server as srv
    except Exception as exc:  # pragma: no cover — dependency-gated
        pytest.skip(f"weaviate_mcp.server unavailable: {exc}")
    return srv


def test_server_builtin_literals_match_ssot() -> None:
    """MUST-MATCH pin: the server keeps its built-in literals (stale-copy
    startup safety) — they may never drift from the SSOT constants."""
    srv = _load_server()
    assert srv._KNOWLEDGE_SUBFOLDERS == kv.BUILTIN_KNOWLEDGE_SUBFOLDERS
    assert dict(srv._NODE_TYPE_TO_FOLDER) == dict(kv.BUILTIN_NODE_TYPE_TO_FOLDER)


def test_sync_builtin_types_match_ssot() -> None:
    """MUST-MATCH pin for the sync template's built-in literal — checked on
    the SOURCE text so it holds even when runtime deps are missing."""
    src = SYNC_SCRIPT_PATH.read_text(encoding="utf-8")
    m = re.search(r"_BUILTIN_NODE_TYPES = frozenset\(\s*\{([^}]*)\}", src)
    assert m, "sync_knowledge_graph.py lost its _BUILTIN_NODE_TYPES literal"
    literal = {t.strip().strip('"\'') for t in m.group(1).split(",") if t.strip()}
    assert frozenset(literal) == kv.BUILTIN_NODE_TYPES


@pytest.fixture()
def server_with_root(tmp_path, monkeypatch):
    """server module pointed (via KG_BASE_DIR) at a tmp project root."""
    srv = _load_server()

    def _point_at(vocabulary_text: str | None) -> Path:
        root = _write_project(tmp_path, vocabulary_text)
        monkeypatch.setattr(srv, "KG_BASE_DIR", str(root))
        kv.clear_vocabulary_cache()
        return root

    return srv, _point_at


def test_declared_custom_folder_trusted_end_to_end(server_with_root) -> None:
    """ACT side: a subfolder declared via `- **Folder**: `thoughts`` is
    trusted exactly like a built-in one — the path passes untouched."""
    srv, point_at = server_with_root
    point_at(FIXTURE_WITH_CUSTOM)
    fp, adjustments = srv._normalize_kg_file_path(
        "knowledge/thoughts/foo.md", "thought", "Foo"
    )
    assert fp == "knowledge/thoughts/foo.md"
    assert adjustments == []


def test_declared_folder_gets_knowledge_prefix(server_with_root) -> None:
    srv, point_at = server_with_root
    point_at(FIXTURE_WITH_CUSTOM)
    fp, adjustments = srv._normalize_kg_file_path("thoughts/foo.md", "thought", "Foo")
    assert fp == "knowledge/thoughts/foo.md"
    assert adjustments == ["prepended 'knowledge/' prefix"]


def test_derived_path_routes_custom_type_to_declared_folder(server_with_root) -> None:
    srv, point_at = server_with_root
    point_at(FIXTURE_WITH_CUSTOM)
    fp, _ = srv._normalize_kg_file_path("", "thought", "A Fleeting Idea")
    assert fp == "knowledge/thoughts/a_fleeting_idea.md"


def test_undeclared_subfolder_is_not_double_prefixed(server_with_root) -> None:
    """LEAVE-ALONE side + the mangling fix: with `thoughts` UNDECLARED the
    path is still corrected, but never nested under a second knowledge/."""
    srv, point_at = server_with_root
    point_at(REAL_TEMPLATE)  # plain template — no `thoughts` declaration
    fp, adjustments = srv._normalize_kg_file_path(
        "knowledge/thoughts/foo.md", "concept", "Foo"
    )
    # The exact pre-#33 mangled output, proven gone:
    assert fp != "knowledge/concepts/knowledge/thoughts/foo.md"
    assert "knowledge/knowledge" not in fp
    assert fp.count("knowledge/") == 1
    # The sane correction: basename re-routed into the node_type's folder.
    assert fp == "knowledge/concepts/foo.md"
    assert any("undeclared" in a for a in adjustments)


def test_knowledge_root_file_is_not_double_prefixed(server_with_root) -> None:
    """knowledge/foo.md (no subfolder at all) also mangled pre-#33."""
    srv, point_at = server_with_root
    point_at(REAL_TEMPLATE)
    fp, _ = srv._normalize_kg_file_path("knowledge/foo.md", "concept", "Foo")
    assert fp == "knowledge/concepts/foo.md"
    assert "knowledge/knowledge" not in fp


def test_builtin_behavior_unchanged_without_declarations(server_with_root) -> None:
    """Regression guard: the historical corrections all still hold when no
    custom declarations exist (missing VOCABULARY.md entirely)."""
    srv, point_at = server_with_root
    point_at(None)
    cases = {
        ("knowledge/concepts/foo.md", "concept"): "knowledge/concepts/foo.md",
        ("concepts/foo.md", "concept"): "knowledge/concepts/foo.md",
        ("foo", "concept"): "knowledge/concepts/foo.md",
        ("bar.md", "project"): "knowledge/projects/bar.md",
        ("baz.md", "insight"): "knowledge/concepts/baz.md",  # unmapped default
    }
    for (path, node_type), expected in cases.items():
        fp, _ = srv._normalize_kg_file_path(path, node_type, "T")
        assert fp == expected, f"{path!r}/{node_type!r} → {fp!r} != {expected!r}"


def test_mid_session_declaration_picked_up_by_normalize(server_with_root) -> None:
    """M3 driving use case at the MCP layer: a long-lived server must honor
    a VOCABULARY.md declaration added mid-session — no restart, no manual
    cache clear between the two calls."""
    srv, point_at = server_with_root
    root = point_at(REAL_TEMPLATE)
    fp, _ = srv._normalize_kg_file_path("knowledge/thoughts/foo.md", "thought", "Foo")
    assert fp == "knowledge/concepts/foo.md"  # not declared yet → re-routed

    vocab_file = root / "knowledge" / "VOCABULARY.md"
    vocab_file.write_text(FIXTURE_WITH_CUSTOM, encoding="utf-8")
    st = vocab_file.stat()  # force a token change on coarse-mtime filesystems
    os.utime(vocab_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    fp2, adjustments = srv._normalize_kg_file_path(
        "knowledge/thoughts/foo.md", "thought", "Foo"
    )
    assert fp2 == "knowledge/thoughts/foo.md"
    assert adjustments == []


def test_stale_vco_lib_degrades_to_builtins_no_crash(
    server_with_root, monkeypatch
) -> None:
    """Version-skew branch: kg_vocabulary unimportable → built-ins only
    (identical pre-#33 behavior), never a startup/normalize crash."""
    srv, point_at = server_with_root
    point_at(FIXTURE_WITH_CUSTOM)  # declared, but the SSOT is unreachable
    monkeypatch.setitem(sys.modules, "vco_lib.kg_vocabulary", None)
    monkeypatch.setattr(srv, "_kg_vocab_import_warned", False)
    fp, _ = srv._normalize_kg_file_path("knowledge/thoughts/foo.md", "concept", "Foo")
    assert fp == "knowledge/concepts/foo.md"  # built-in fallback, no nesting
    assert "knowledge/knowledge" not in fp


# ═════════════════════════════════════════════════════════════════════════════
# Consumer 2 — templates/scripts/sync_knowledge_graph.py
# ═════════════════════════════════════════════════════════════════════════════

def _load_sync_module(monkeypatch, project_root: Path):
    """Import the sync template with a controlled env (the established
    importlib pattern — see test_v0289_kg_sync_project_root.py)."""
    monkeypatch.setenv("KG_SYNC_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("KG_COLLECTION", "V0291VocabTestKG")
    monkeypatch.setenv("DEVELOPMENT_COLLECTION", "")
    monkeypatch.setenv("VCT_DISABLE_HUB_RESOLVER", "1")
    monkeypatch.delenv("KG_BASE_DIR", raising=False)
    # vco_lib must resolve from THIS repo, not a foreign orchestrator root.
    monkeypatch.delenv("VCT_ORCHESTRATOR_ROOT", raising=False)

    mod_name = f"_sync_kg_vocab_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, SYNC_SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as exc:  # pragma: no cover — dependency-gated
        pytest.skip(f"sync_knowledge_graph.py runtime deps missing ({exc})")
    return mod


def _type_warnings(mod, node_type: str, file_path: Path) -> list:
    """Validator warnings about the TYPE only (tags kept valid to isolate)."""
    node = {
        "node_type": node_type,
        "tags": ["AI", "workflow", "mid-level-architecture"],
    }
    return [
        w for w in mod.validate_node_against_vocabulary(node, file_path)
        if "not declared" in w
    ]


def test_declared_custom_type_accepted_by_validator(tmp_path, monkeypatch) -> None:
    """ACT side: a VOCABULARY.md-declared type passes validation."""
    root = _write_project(tmp_path, FIXTURE_WITH_CUSTOM)
    mod = _load_sync_module(monkeypatch, root)
    assert _type_warnings(mod, "thought", root / "knowledge" / "n.md") == []


def test_undeclared_type_still_warns(tmp_path, monkeypatch) -> None:
    """LEAVE-ALONE side: an undeclared type keeps warning, and the warning
    teaches the declaration pattern."""
    root = _write_project(tmp_path, FIXTURE_WITH_CUSTOM)
    mod = _load_sync_module(monkeypatch, root)
    warnings = _type_warnings(mod, "daydream", root / "knowledge" / "n.md")
    assert len(warnings) == 1
    assert "VOCABULARY.md" in warnings[0]


def test_relationship_sections_not_accepted_as_types(tmp_path, monkeypatch) -> None:
    """The REAL template's relationship sections (`#### **`uses`**
    (co:uses)` shape) never widen the validator's accepted set."""
    root = _write_project(tmp_path, REAL_TEMPLATE)
    mod = _load_sync_module(monkeypatch, root)
    assert _type_warnings(mod, "uses", root / "knowledge" / "n.md"), (
        "'uses' (a relationship, not a class) was accepted as a node type"
    )
    assert mod._load_vocabulary_node_types() == kv.BUILTIN_NODE_TYPES


def test_fallback_parser_parity_with_ssot(tmp_path, monkeypatch) -> None:
    """PARITY PIN: the sync template's inline fallback parser must extract
    the same OPEN type set as the SSOT for the same input (must-match
    comments at both sites point here)."""
    root = _write_project(tmp_path, REAL_TEMPLATE)
    mod = _load_sync_module(monkeypatch, root)
    prose_foil = (
        "\nDeclare custom types in knowledge/VOCABULARY.md as a class "
        "heading with (alias: `daydream`)\n"
    )
    samples = (
        REAL_TEMPLATE,
        FIXTURE_WITH_CUSTOM,
        FIXTURE_WITH_CUSTOM + prose_foil,
        "not markdown at all",
    )
    for text in samples:
        inline = frozenset(mod._BUILTIN_NODE_TYPES | mod._parse_vocabulary_types_fallback(text))
        ssot = kv.parse_vocabulary_text(text).node_types
        assert inline == ssot, (
            f"fallback parser diverged from vco_lib.kg_vocabulary for "
            f"sample starting {text[:60]!r}: {sorted(inline ^ ssot)}"
        )


def test_import_fallback_branch_still_validates_declared_types(
    tmp_path, monkeypatch, capsys
) -> None:
    """Version-skew branch live: with vco_lib.kg_vocabulary blocked, the
    inline parser serves the SAME open set and warns exactly once."""
    root = _write_project(tmp_path, FIXTURE_WITH_CUSTOM)
    mod = _load_sync_module(monkeypatch, root)
    monkeypatch.setitem(sys.modules, "vco_lib.kg_vocabulary", None)
    mod._VOCABULARY_TYPES_CACHE = None
    mod._VOCAB_IMPORT_WARNED = False

    types = mod._load_vocabulary_node_types()
    assert "thought" in types
    assert kv.BUILTIN_NODE_TYPES <= types
    err = capsys.readouterr().err
    assert "kg_vocabulary unavailable" in err

    # Warn ONCE: a second (cache-cleared) resolution stays quiet.
    mod._VOCABULARY_TYPES_CACHE = None
    mod._load_vocabulary_node_types()
    assert "kg_vocabulary unavailable" not in capsys.readouterr().err


def test_unreadable_vocabulary_builtins_only_no_crash(tmp_path, monkeypatch) -> None:
    """UnicodeDecodeError path through the whole validator: mis-encoded
    ontology → built-ins only, kg-sync never crashes."""
    root = _write_project(tmp_path, None)
    (root / "knowledge" / "VOCABULARY.md").write_bytes(b"\xff\xfe garbage \x80")
    mod = _load_sync_module(monkeypatch, root)
    assert mod._load_vocabulary_node_types() == kv.BUILTIN_NODE_TYPES
    # Same guarantee on the skew fallback branch (its own read path).
    monkeypatch.setitem(sys.modules, "vco_lib.kg_vocabulary", None)
    mod._VOCABULARY_TYPES_CACHE = None
    mod._VOCAB_IMPORT_WARNED = True  # silence the once-warning; read path under test
    assert mod._load_vocabulary_node_types() == kv.BUILTIN_NODE_TYPES
