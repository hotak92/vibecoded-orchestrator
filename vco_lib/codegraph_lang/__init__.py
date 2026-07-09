# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-language code-graph extractor modules (P2f stage 2, v0.2.76).

Extractor logic for ``templates/scripts/analyze_code_graph.py`` lives HERE —
new languages / extractor features go in a ``vco_lib/codegraph_lang/<lang>.py``
module the analyzer imports loud-fail (no inline extractors in the analyzer,
no silent import fallbacks).

Shape
-----
Each language module exposes ``analyze_<lang>_file(ctx, file_path, repo_root)
-> dict`` — a verbatim move of the former
``CodeGraphAnalyzer._analyze_<lang>_file`` method where ``ctx`` IS the
analyzer instance. The write path, unchanged-skip gate, caches, and the
embedding seams are reached via ``ctx.`` (``ctx.store_entity`` /
``ctx._create_or_update_module`` / ``ctx._get_existing_module`` /
``ctx._store_interactions`` / ``ctx.embed_function`` …; see the analyzer's
"module-global seams" block for the last group). The extractors write
entities imperatively MID-WALK and return the per-file stats dict — that
imperative write order is pinned by the golden snapshot suite
(``tests/test_codegraph_golden.py``). Do NOT convert them to pure
entity-producers (``-> list[CodeEntity]``) as a drive-by: that reorders the
write/cache lifecycle and is explicitly deferred to a future stage with its
own golden-diff review budget.

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

from vco_lib.codegraph_lang.powershell import analyze_powershell_file
from vco_lib.codegraph_lang.python import analyze_python_file

#: language-dispatch key -> extractor callable
#: ``(ctx, file_path, repo_root) -> stats dict``.
EXTRACTORS: Dict[str, Callable[[Any, Path, Path], Dict[str, int]]] = {
    "python": analyze_python_file,
    "powershell": analyze_powershell_file,
}
