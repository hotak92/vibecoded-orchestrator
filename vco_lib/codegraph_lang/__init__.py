# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-language code-graph extractor modules (P2f stage 2, v0.2.76).

Extractor logic for ``templates/scripts/analyze_code_graph.py`` lives HERE —
new languages / extractor features go in a ``vco_lib/codegraph_lang/<lang>.py``
module the analyzer imports loud-fail (no inline extractors in the analyzer,
no silent import fallbacks).

Shape (P2f stage 3, v0.2.77 Part 6 — PURE PRODUCERS)
----------------------------------------------------
Each language module exposes TWO callables:

* ``extract_<lang>_file(source_text, file_path, repo_root, helpers) ->
  FileExtraction`` — the PURE PRODUCER. It reads source and RETURNS a
  ``FileExtraction`` (module descriptor + entities in emission order +
  interactions + imports + stats); it mutates NO analyzer state. Embedding is
  reached via the narrow ``helpers`` protocol
  (``vco_lib.codegraph_lang._shared.ExtractorHelpers`` — embed seams + the
  python-only AST passthroughs), never the analyzer instance directly.
* ``analyze_<lang>_file(ctx, file_path, repo_root) -> dict`` — the THIN SHIM
  the dispatch table still calls (``EXTRACTORS`` maps to THIS). Via the shared
  ``_shared.run_pure_extractor`` it runs the analyzer-side skip gates
  (minified-content + the unchanged-file ``ctx._get_existing_module`` gate —
  BEFORE extraction, preserving the short-circuit), then
  ``extract_<lang>_file`` -> ``ctx.write_file_extraction`` -> stats dict.

ONE writer — ``CodeGraphAnalyzer.write_file_extraction`` — owns every
side-effect (module upsert incl. the ``data.update`` LANDMINE bypass,
``module_imports`` cache, entity writes through
``store_entity``/``_dedup_insert``, class/function cache capture, and
``_store_interactions``) in the EXACT pre-Part-6 order. This is the successor
to the v0.2.76 Part-3 CORRECTION-v2 deferral: the write/cache lifecycle is now
single-homed and reviewable, no longer scattered as imperative ``ctx.`` calls
across the extractors. Byte-identity of the stored output is pinned by the
golden snapshot suite (``tests/test_codegraph_golden.py``) — any diff is a
writer-ordering defect to FIX, never a regen.

Per-language PRIVATE helpers (regexes, per-language parsers/constants) live in
their language's module. Helpers shared ACROSS the extractors (and used by
nothing else) live in ``_shared``. Helpers shared with non-extractor analyzer
code (the embedding seams) stay in the analyzer.

Registry
--------
``EXTRACTORS`` maps the analyzer's ``lang_dispatch`` language keys (the same
keys ``_EXT_TO_DISPATCH_NAME`` resolves file extensions to) to the extractor
callables. The analyzer's per-language delegator methods dispatch through it.
Adding a language = one module here + one ``EXTRACTORS`` entry + one
``lang_dispatch`` row (+ finder spec) in the analyzer.

Populated per-language as the P2f stage-2 moves land; registry↔dispatch
parity is pinned by ``tests/test_codegraph_lang_scaffold.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict

from vco_lib.codegraph_lang.cpp import analyze_cpp_file
from vco_lib.codegraph_lang.csharp import analyze_csharp_file
from vco_lib.codegraph_lang.go import analyze_go_file
from vco_lib.codegraph_lang.java import analyze_java_file
from vco_lib.codegraph_lang.javascript import analyze_js_file
from vco_lib.codegraph_lang.lua import analyze_lua_file
from vco_lib.codegraph_lang.powershell import analyze_powershell_file
from vco_lib.codegraph_lang.proto import analyze_proto_file
from vco_lib.codegraph_lang.python import analyze_python_file
from vco_lib.codegraph_lang.ruby import analyze_ruby_file
from vco_lib.codegraph_lang.rust import analyze_rust_file
from vco_lib.codegraph_lang.shell import analyze_shell_file
from vco_lib.codegraph_lang.svelte import analyze_svelte_file

#: language-dispatch key -> extractor callable
#: ``(ctx, file_path, repo_root) -> stats dict``.
EXTRACTORS: Dict[str, Callable[[Any, Path, Path], Dict[str, int]]] = {
    "python": analyze_python_file,
    "powershell": analyze_powershell_file,
    "rust": analyze_rust_file,
    # One extractor serves both dispatch keys (mirrors lang_dispatch).
    "javascript": analyze_js_file,
    "typescript": analyze_js_file,
    "go": analyze_go_file,
    "java": analyze_java_file,
    "csharp": analyze_csharp_file,
    "cpp": analyze_cpp_file,
    "ruby": analyze_ruby_file,
    "lua": analyze_lua_file,
    "shell": analyze_shell_file,
    "proto": analyze_proto_file,
    "svelte": analyze_svelte_file,
}
