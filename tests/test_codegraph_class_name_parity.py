# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.74 (BLOCKER-1) — CODE-GRAPH writer/reader class-name parity.

The analyzer WRITES Code* collections with the underscore-PRESERVING
`canonical_class_prefix` (via its own `_collection_name` / `_sanitize_
collection_prefix`, which delegates to `canonical_class_prefix`). The MCP READER
must resolve the SAME class name, so it now uses a code-graph-ONLY sanitizer
`weaviate_mcp.server._code_sanitize_collection_prefix` that ALSO delegates to
`canonical_class_prefix` — NOT the shared underscore-DROPPING
`_sanitize_collection_prefix` (that one is for diagrams/KG and is pinned
separately by `test_diagrams_class_name_parity.py`).

Before this split the reader dropped underscores, so for ANY underscored project
name the MCP queried a DIFFERENT class than the analyzer wrote → silent
0-results + a latent duplicate collection. This test pins writer==reader so that
regression can't return. It deliberately does NOT touch the diagrams parity
fixture — the two sanitizers are intentionally different and must stay so.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_MCP_ROOT = REPO_ROOT / "claude_mcp_servers"
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

# The analyzer's writer-side prefix SSOT.
from vco_lib.project_naming import canonical_class_prefix  # noqa: E402


def _load_analyzer():
    path = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
    spec = importlib.util.spec_from_file_location("_parity_analyzer", str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:
        pytest.skip("weaviate-client unavailable — analyzer cannot load")
    return mod


def _load_server():
    from weaviate_mcp import server as srv
    return srv


# Names chosen to exercise underscores (the previously-broken case), plus
# spaces / hyphens / mixed case + the non-underscored control.
_NAMES = [
    "VibeCodedOrchestrator",   # control — no underscore, always matched
    "My_Project",
    "foo_bar_baz",
    "Camel_Case",
    "a-b c",
    "SD15",
    "Client_A_Private",
]


@pytest.mark.parametrize("name", _NAMES)
def test_reader_prefix_equals_writer_prefix(name):
    """The MCP code-graph reader sanitizer must equal the analyzer's writer
    prefix (canonical_class_prefix) for every project name."""
    srv = _load_server()
    writer = canonical_class_prefix(name)
    reader = srv._code_sanitize_collection_prefix(name)
    assert reader == writer, (
        f"code-graph writer/reader prefix MISMATCH for {name!r}: "
        f"writer(analyzer)={writer!r} vs reader(mcp)={reader!r}. The MCP's "
        f"_code_sanitize_collection_prefix must delegate to canonical_class_prefix "
        f"(underscore-PRESERVING) — do NOT route code-graph through the "
        f"underscore-dropping _sanitize_collection_prefix."
    )


@pytest.mark.parametrize("name", _NAMES)
def test_analyzer_writer_prefix_equals_canonical(name):
    """Guard the writer side too: the analyzer's own prefix helper must equal
    canonical_class_prefix (so the parity above is anchored on the real writer)."""
    mod = _load_analyzer()
    writer_via_analyzer = mod._sanitize_collection_prefix(name)
    assert writer_via_analyzer == canonical_class_prefix(name), (
        f"analyzer writer prefix drifted from canonical_class_prefix for {name!r}"
    )


def test_code_and_diagrams_sanitizers_diverge_on_underscore():
    """Sanity: the two sanitizers are DELIBERATELY different — an underscored
    name PRESERVES for code-graph and DROPS for diagrams/KG. If they ever
    coincide for an underscored name, the split has silently collapsed."""
    srv = _load_server()
    name = "My_Project"
    code = srv._code_sanitize_collection_prefix(name)      # preserving
    diagrams = srv._sanitize_collection_prefix(name)       # dropping
    assert code == "My_Project"
    assert diagrams == "MyProject"
    assert code != diagrams, (
        "code-graph and diagrams/KG sanitizers must diverge on underscored names"
    )
