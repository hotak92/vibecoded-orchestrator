# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P2f stage 2 (v0.2.76) scaffold guards: the analyzer↔vco_lib/codegraph_lang
seams the per-language extractor moves depend on.

1. THE DELEGATOR SEAM. The extractors move out of the analyzer and receive
   the analyzer instance as ``ctx``. Four module-global dependencies stay in
   the analyzer (``embed_function`` / ``embed_class`` / ``generate_embedding``
   / ``_shape_for_insert`` — shared with non-extractor code + embedding-service
   module state), re-exposed as instance delegators that resolve the module
   global LATE. These tests pin the load-bearing property: monkeypatching
   ``analyzer_mod.<name>`` (the seam the golden suite and a dozen other tests
   use) must govern calls made through ``ctx.<name>`` from the moved
   extractors. If a delegator ever binds EARLY (e.g. staticmethod capture),
   the stub stops reaching the moved extractors and embedding calls silently
   go live in tests — these tests fail loudly instead.

2. THE REGISTRY. ``vco_lib.codegraph_lang.EXTRACTORS`` maps the analyzer's
   ``lang_dispatch`` language keys to extractor callables. While the
   per-language moves land the registry grows; every entry must already be
   consistent (callable, key known to the analyzer's extension dispatch).
   The final full-parity pin (registry keys == dispatch keys) lands with the
   last move commit.
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_codegraph_lang_scaffold_acg", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _bare_instance(analyzer_mod: types.ModuleType):
    """An analyzer instance without __init__ (no Weaviate) — the same idiom
    the golden suite uses."""
    return analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)


# ---------------------------------------------------------------------------
# 1 — delegator seam: instance calls resolve the CURRENT module global.
# ---------------------------------------------------------------------------


def test_embed_function_delegator_sees_module_stub(analyzer_mod, monkeypatch):
    sentinel = object()
    seen = {}

    def _stub(signature, body, language="python"):
        seen["args"] = (signature, body, language)
        return sentinel

    monkeypatch.setattr(analyzer_mod, "embed_function", _stub)
    inst = _bare_instance(analyzer_mod)
    assert inst.embed_function("sig", "body", language="lua") is sentinel
    assert seen["args"] == ("sig", "body", "lua")


def test_embed_class_delegator_sees_module_stub(analyzer_mod, monkeypatch):
    sentinel = object()
    monkeypatch.setattr(
        analyzer_mod, "embed_class",
        lambda sig, body, methods=None, language="python": sentinel,
    )
    inst = _bare_instance(analyzer_mod)
    assert inst.embed_class("sig", "body", methods=["m"], language="rust") is sentinel


def test_generate_embedding_delegator_sees_module_stub(analyzer_mod, monkeypatch):
    monkeypatch.setattr(analyzer_mod, "generate_embedding", lambda text: [1.0, 2.0])
    inst = _bare_instance(analyzer_mod)
    assert inst.generate_embedding("some text") == [1.0, 2.0]


def test_shape_for_insert_delegator_sees_module_stub(analyzer_mod, monkeypatch):
    monkeypatch.setattr(analyzer_mod, "_shape_for_insert", lambda emb: {"shaped": emb})
    inst = _bare_instance(analyzer_mod)
    assert inst._shape_for_insert([0.5]) == {"shaped": [0.5]}


def test_delegators_unstubbed_hit_real_module_functions(analyzer_mod):
    """Without stubs, the delegator resolves the REAL module function —
    `_shape_for_insert` is pure (no embedding service needed for the
    None/dict passthrough branches), so exercise it end-to-end."""
    inst = _bare_instance(analyzer_mod)
    assert inst._shape_for_insert(None) is None
    slots = {"codesage_embed": [0.1]}
    assert inst._shape_for_insert(slots) is slots


# ---------------------------------------------------------------------------
# 2 — registry shape.
# ---------------------------------------------------------------------------


def test_registry_entries_are_callable_and_keys_known(analyzer_mod):
    from vco_lib import codegraph_lang

    assert isinstance(codegraph_lang.EXTRACTORS, dict)
    # Every registered key must be a language the analyzer's extension
    # dispatch knows (same key space as lang_dispatch).
    known_keys = set(analyzer_mod._EXT_TO_DISPATCH_NAME.values())
    for key, fn in codegraph_lang.EXTRACTORS.items():
        assert callable(fn), f"EXTRACTORS[{key!r}] is not callable"
        assert key in known_keys, (
            f"EXTRACTORS key {key!r} unknown to the analyzer's "
            "_EXT_TO_DISPATCH_NAME dispatch key space"
        )
