# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.70 Stream D — KG-presentation fixes (formatter-only, no schema change).

D-1: a Development-collection result with NO `node_type` property must render as
     node_type="doc" (NOT "unknown"). True KG nodes that legitimately lack a type
     keep "unknown" — the "doc" default is GATED on the Development collection.
     (The docs collection deliberately has no node_type property — adding it was
     rejected; this is the formatter-only fix in _format_obj.)
D-2: `file_path` must appear in rl_kg_search.py --hook-format output (as a
     "| src=<path>" trailer) so injected blocks are openable + the seen-store
     reads-ledger can match.
D-3: pre-bash-context-inject must strip command-noise tokens before building the
     KG query (so a bare cd/ls doesn't inject directory-keyword KG).
"""
from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
HOOKS = REPO_ROOT / "templates" / "hooks"


@pytest.fixture
def fresh_server(monkeypatch) -> Iterator:
    for p in (str(MCP_DIR), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    for name in [n for n in list(sys.modules) if n == "weaviate_mcp" or n.startswith("weaviate_mcp.")]:
        sys.modules.pop(name, None)

    def _do_import():
        try:
            return importlib.import_module("weaviate_mcp.server")
        except Exception as exc:
            pytest.fail(f"weaviate_mcp.server import failed: {exc}")

    yield _do_import

    for name in list(sys.modules):
        if name == "weaviate_mcp" or name.startswith("weaviate_mcp."):
            sys.modules.pop(name, None)


def _set_env(monkeypatch, **kwargs: str) -> None:
    for key, value in kwargs.items():
        if value is None or value == "":
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _make_obj(**properties) -> SimpleNamespace:
    metadata = SimpleNamespace(distance=properties.pop("distance", 0.1))
    return SimpleNamespace(properties=dict(properties), metadata=metadata)


# --------------------------------------------------------------------------
# D-1 — node_type "doc" default for Development-collection results
# --------------------------------------------------------------------------
def test_d1_development_result_defaults_to_doc(monkeypatch, fresh_server):
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="MyProject_Development",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_HUB_TOKEN="",
    )
    server = fresh_server()
    assert server.DEVELOPMENT_COLLECTION == "MyProject_Development"
    # A docs object with NO node_type property.
    obj = _make_obj(title="Some Doc", content="hello", file_path="docs/foo.md")
    formatted = server._format_obj(obj, "MyProject_Development", distance=0.1)
    assert formatted["node_type"] == "doc", (
        f"Development result must default node_type to 'doc'; got "
        f"{formatted['node_type']!r}"
    )


def test_d1_kg_result_without_type_stays_unknown(monkeypatch, fresh_server):
    """A true KG-collection result lacking node_type must NOT silently become
    'doc' — it keeps the KG-appropriate 'unknown' default."""
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="MyProject_Development",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_HUB_TOKEN="",
    )
    server = fresh_server()
    obj = _make_obj(title="KG Node", content="x", file_path="knowledge/foo.md")
    formatted = server._format_obj(obj, "MyProject_KnowledgeGraph", distance=0.1)
    assert formatted["node_type"] == "unknown", (
        f"KG result without a type must stay 'unknown', never 'doc'; got "
        f"{formatted['node_type']!r}"
    )


def test_d1_explicit_node_type_preserved(monkeypatch, fresh_server):
    """An explicit node_type is preserved on BOTH collections."""
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="MyProject_Development",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_HUB_TOKEN="",
    )
    server = fresh_server()
    obj = _make_obj(title="Typed", node_type="concept", content="x", file_path="k/foo.md")
    fmt_dev = server._format_obj(obj, "MyProject_Development", distance=0.1)
    fmt_kg = server._format_obj(obj, "MyProject_KnowledgeGraph", distance=0.1)
    assert fmt_dev["node_type"] == "concept"
    assert fmt_kg["node_type"] == "concept"


def test_d1_no_development_collection_configured(monkeypatch, fresh_server):
    """With DEVELOPMENT_COLLECTION unset, no result is mis-stamped 'doc'."""
    _set_env(
        monkeypatch,
        KG_COLLECTION="MyProject_KnowledgeGraph",
        SHARED_KG_COLLECTION="",
        DEVELOPMENT_COLLECTION="",
        DIAGRAMS_COLLECTION="",
        VCT_KG_ACCESS_LIST="",
        VCT_HUB_TOKEN="",
    )
    server = fresh_server()
    obj = _make_obj(title="N", content="x", file_path="k/foo.md")
    formatted = server._format_obj(obj, "MyProject_KnowledgeGraph", distance=0.1)
    assert formatted["node_type"] == "unknown"


# --------------------------------------------------------------------------
# D-2 — file_path in rl_kg_search.py --hook-format
# --------------------------------------------------------------------------
def test_d2_rl_kg_search_emits_src_trailer() -> None:
    """rl_kg_search.py --hook-format must append a '| src=<file_path>' trailer
    (only in hook-format mode, only when a path exists)."""
    src = (MCP_DIR / "scripts" / "rl_kg_search.py").read_text(encoding="utf-8")
    assert 'file_path = entry.get("file_path", "")' in src, (
        "rl_kg_search.py must read file_path from the entry dict"
    )
    assert 'src_trailer = f" | src={file_path}"' in src, (
        "rl_kg_search.py must build a '| src=<path>' trailer for hook-format"
    )
    assert "args.hook_format and file_path" in src, (
        "the src trailer must be gated on hook-format mode AND a non-empty path"
    )
    # All three render branches must append the trailer.
    assert src.count("{src_trailer}") >= 3, (
        "all three --hook-format render branches must append the src trailer"
    )


# --------------------------------------------------------------------------
# D-3 — command-noise strip in pre-bash KG query
# --------------------------------------------------------------------------
def _has_bash() -> bool:
    return shutil.which("bash") is not None


def test_d3_prebash_sources_shared_strip_no_inline() -> None:
    """N-3 (one home), v0.2.101 RETARGET: the pre-bash wrapper no longer
    BUILDS a query at all — command-text noise-stripping (and the 500-char
    THRESHOLD it fed) were retired with §C1: the router classifies the
    command and builds queries from target paths/symbols ONLY
    (vco_lib/inject_intent.py — never from command text). The one-home
    property this row guarded ("no inline strip copy in the hook") is now
    the stronger "no query build in the hook at all"; the retired shell
    strip lib awaits Wave-3 removal with its remaining consumers."""
    for ext in (".sh", ".ps1"):
        body = (HOOKS / f"pre-bash-context-inject{ext}").read_text(encoding="utf-8")
        executable = "\n".join(
            ln for ln in body.splitlines()
            if not ln.lstrip().startswith(("#", "<#"))
        )
        assert "vco_strip_command_noise" not in executable, (
            f"pre-bash{ext}: the noise-strip call must stay retired — the "
            "router owns query building"
        )
        assert "Get-VcoCommandNoiseStripped" not in executable, (
            f"pre-bash{ext}: the noise-strip call must stay retired"
        )
        assert "for t in cmd.split()" not in body, (
            f"pre-bash{ext} must NOT carry an inline copy of the noise-strip"
        )
        assert "hook_context_router" in executable, (
            f"pre-bash{ext} must drive the router (the query-building home)"
        )


# v0.2.101 wave-2 review SF-2: _lib/command-noise-strip.{sh,ps1} were
# RETIRED with their last caller (the pre-bash query build — queries are
# built from targets/symbols now, never from command text). The D-3 rows
# that drove the lib were retired with it; the wrapper-side retirement pin
# (test_d3_prebash_sources_shared_strip_no_inline above) survives.
