# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE home for loading the code graph's tree-sitter grammars (v0.2.102 W6-E).

Two consumers parse source with tree-sitter:

* :mod:`vco_lib.codegraph_calls` — CALL-name extraction for the ``calls`` edge
  (since v0.2.77);
* :mod:`vco_lib.codegraph_defs` — the DEFINITIONS layer (classes, functions,
  methods, line ranges) the per-language extractors in
  :mod:`vco_lib.codegraph_lang` use first (since v0.2.102).

Both load grammars through :func:`load_language` here, so the grammar table,
the per-process cache and the once-per-language "unavailable" log line exist
exactly once.

Soft-fail contract
------------------
``tree-sitter`` and its grammar wheels are the ``codegraph-ts`` extra
(``pyproject.toml``). ``install.py`` installs it by default
(``_install_codegraph_treesitter``; opt out with ``VCT_SKIP_CODEGRAPH_TS=1``),
but an air-gapped or opted-out install is SUPPORTED: a grammar that cannot be
imported or instantiated makes :func:`load_language` return ``None`` (logged
once per language per process) and each consumer keeps its pre-tree-sitter
behaviour — no call edges for that language, regex definitions. This is the
one sanctioned exception to "loud-fail over silent degrade", because the extra
is optional by design.

Grammars NOT in the extra (no wheel is pinned): PowerShell, Protocol Buffers
and Svelte's own template grammar. Those languages never reach this module's
table — their extractors stay regex (Svelte's ``<script>`` blocks are parsed
with the TypeScript grammar by :mod:`vco_lib.codegraph_defs`).
"""
from __future__ import annotations

import importlib
import logging
import threading
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: canonical grammar id -> (grammar module, factory attribute). The ids are
#: the analyzer's canonical language ids, plus ``tsx`` (TypeScript + JSX —
#: ``.tsx`` files need the separate factory; ``.jsx`` parses with the
#: JavaScript grammar, which includes JSX).
#:
#: Every module here is pinned in ``pyproject.toml [codegraph-ts]`` — keep the
#: two in step (``tests/test_codegraph_defs.py`` pins the parity).
GRAMMARS: Dict[str, Tuple[str, str]] = {
    "python": ("tree_sitter_python", "language"),
    "rust": ("tree_sitter_rust", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "csharp": ("tree_sitter_c_sharp", "language"),
    "cpp": ("tree_sitter_cpp", "language"),
    "ruby": ("tree_sitter_ruby", "language"),
    "lua": ("tree_sitter_lua", "language"),
    "shell": ("tree_sitter_bash", "language"),
}

# lang -> loaded ``tree_sitter.Language`` or ``None`` ("tried; unavailable").
# Membership distinguishes "not attempted" from "attempted and failed", so a
# missing grammar is probed and logged exactly once per process.
_LANGUAGES: Dict[str, Optional[Any]] = {}
_LOCK = threading.Lock()


def _try_load(lang: str) -> Optional[Any]:
    spec = GRAMMARS.get(lang)
    if spec is None:
        return None
    mod_name, factory_attr = spec
    try:
        from tree_sitter import Language  # type: ignore

        grammar_mod = importlib.import_module(mod_name)
        return Language(getattr(grammar_mod, factory_attr)())
    except Exception as exc:  # noqa: BLE001 — soft-fail: ImportError and any
        # grammar-load / ABI error leave the language on its fallback path.
        logger.info(
            "codegraph_grammars: tree-sitter grammar unavailable for '%s' "
            "(%s: %s); the code graph uses its non-tree-sitter fallback for "
            "this language. Install the optional extra to enable it: "
            "pip install '.[codegraph-ts]'",
            lang, type(exc).__name__, exc,
        )
        return None


def load_language(lang: str) -> Optional[Any]:
    """Return the cached ``tree_sitter.Language`` for ``lang``, or ``None``
    when the core or the grammar wheel cannot be loaded (logged once)."""
    if lang in _LANGUAGES:
        return _LANGUAGES[lang]
    with _LOCK:
        if lang not in _LANGUAGES:
            _LANGUAGES[lang] = _try_load(lang)
        return _LANGUAGES[lang]


def parse(lang: str, source: bytes) -> Optional[Any]:
    """Parse ``source`` (UTF-8 bytes) with ``lang``'s grammar.

    Returns the ``tree_sitter.Tree``, or ``None`` when the grammar is
    unavailable or the parser raised (logged; the caller falls back).
    """
    language = load_language(lang)
    if language is None:
        return None
    try:
        from tree_sitter import Parser  # type: ignore

        return Parser(language).parse(source)
    except Exception as exc:  # noqa: BLE001 — a parser failure is per-file
        logger.info(
            "codegraph_grammars: tree-sitter parse failed for '%s' (%s: %s)",
            lang, type(exc).__name__, exc,
        )
        return None
