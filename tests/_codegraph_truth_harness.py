# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared harness for the code-graph DEFINITIONS correctness gate
(``tests/test_codegraph_definitions_truth.py``, v0.2.102 W6-E).

Runs a language's PURE extractor (``extract_<lang>_file``) over the committed
fixture files under ``tests/fixtures/codegraph_truth/<lang>/`` with a stub
helpers object (no embedding, no Weaviate, no writes) and scores the extracted
class/function symbols against each fixture's committed, hand-reviewed
``<file>.truth.json``.

A symbol is ``(kind, name, start_line, end_line)``; precision and recall are
multiset matches, so a wrong line RANGE counts as both a false positive and a
false negative — the truncated-body and runaway-scan defects score as misses,
not as hits.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from vco_lib.codegraph_entities import KIND_CLASS, KIND_FUNCTION
from vco_lib.codegraph_lang.cpp import extract_cpp_file
from vco_lib.codegraph_lang.csharp import extract_csharp_file
from vco_lib.codegraph_lang.go import extract_go_file
from vco_lib.codegraph_lang.java import extract_java_file
from vco_lib.codegraph_lang.javascript import extract_js_file
from vco_lib.codegraph_lang.lua import extract_lua_file
from vco_lib.codegraph_lang.ruby import extract_ruby_file
from vco_lib.codegraph_lang.rust import extract_rust_file
from vco_lib.codegraph_lang.shell import extract_shell_file
from vco_lib.codegraph_lang.svelte import extract_svelte_file

TRUTH_ROOT = Path(__file__).parent / "fixtures" / "codegraph_truth"
THRESHOLD = 0.95

#: fixture sub-directory -> (pure extractor, tree-sitter grammar id it needs)
EXTRACTORS: Dict[str, Tuple[Callable, str]] = {
    "typescript": (extract_js_file, "typescript"),
    "javascript": (extract_js_file, "javascript"),
    "rust": (extract_rust_file, "rust"),
    "go": (extract_go_file, "go"),
    "java": (extract_java_file, "java"),
    "csharp": (extract_csharp_file, "csharp"),
    "cpp": (extract_cpp_file, "cpp"),
    "ruby": (extract_ruby_file, "ruby"),
    "lua": (extract_lua_file, "lua"),
    "shell": (extract_shell_file, "shell"),
    "svelte": (extract_svelte_file, "typescript"),
}

Symbol = Tuple[str, str, int, int]


class StubHelpers:
    """The narrow ExtractorHelpers surface, embedding disabled."""

    project_name = "TruthProj"

    def embed_class(self, *a, **k):  # noqa: ANN002,ANN003,ANN201
        return None

    def embed_function(self, *a, **k):  # noqa: ANN002,ANN003,ANN201
        return None

    def generate_embedding(self, *a, **k):  # noqa: ANN002,ANN003,ANN201
        return None


def fixture_files(lang: str) -> List[Path]:
    d = TRUTH_ROOT / lang
    return sorted(p for p in d.iterdir() if p.is_file() and not p.name.endswith(".truth.json"))


def truth_path(fixture: Path) -> Path:
    return fixture.with_name(fixture.name + ".truth.json")


def extracted_symbols(lang: str, fixture: Path) -> List[Symbol]:
    extractor, _grammar = EXTRACTORS[lang]
    text = fixture.read_text(encoding="utf-8")
    fx = extractor(text, fixture, TRUTH_ROOT, StubHelpers())
    out: List[Symbol] = []
    for e in fx.entities:
        if e.kind == KIND_CLASS:
            out.append(("class", e.name, e.start_line, e.end_line))
        elif e.kind == KIND_FUNCTION:
            out.append(("function", e.name, e.start_line, e.end_line))
    return out


def truth_symbols(fixture: Path) -> List[Symbol]:
    rows = json.loads(truth_path(fixture).read_text(encoding="utf-8"))["symbols"]
    return [(r["kind"], r["name"], r["start"], r["end"]) for r in rows]


def force_regex_definitions(monkeypatch) -> None:  # noqa: ANN001
    """Make every extractor take its REGEX fallback for one test (as on an
    install without the ``codegraph-ts`` extra)."""
    from vco_lib import codegraph_defs

    monkeypatch.setattr(codegraph_defs, "parse", lambda lang, data: None)


def treesitter_available(*grammars: str) -> bool:
    """True when every named tree-sitter grammar loads in this process."""
    from vco_lib import codegraph_grammars

    return all(codegraph_grammars.load_language(g) is not None for g in grammars)


def score(lang: str) -> Tuple[float, float, int, int, int]:
    """(precision, recall, true_positives, extracted, truth) over every
    fixture of ``lang``."""
    tp = n_ext = n_truth = 0
    for fixture in fixture_files(lang):
        ext = Counter(extracted_symbols(lang, fixture))
        tru = Counter(truth_symbols(fixture))
        tp += sum((ext & tru).values())
        n_ext += sum(ext.values())
        n_truth += sum(tru.values())
    precision = tp / n_ext if n_ext else (1.0 if not n_truth else 0.0)
    recall = tp / n_truth if n_truth else 1.0
    return precision, recall, tp, n_ext, n_truth
