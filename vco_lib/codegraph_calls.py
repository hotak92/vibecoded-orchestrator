# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE facade for code-graph CALL-name extraction across languages (CG-2 /
V52-O.11.G, v0.2.77 Part 5).

Why this module exists
----------------------
The ``calls`` edge on ``CodeFunction`` is built by the analyzer's
post-processing pass (``analyze_code_graph.py::create_cross_references``),
which re-parses each stored function body to list the names it calls. Until
this module, that re-parse was **Python ``ast`` only** — so all twelve
non-Python languages the analyzer walks got *zero* call edges, and the MCP
surfaced the gap with the ``unsupported_for_language`` marker (``callers`` /
``path`` queries returning empty on a non-Python target).

This facade closes that gap with an **optional** ``tree-sitter`` dependency:

* **Python** keeps its dependency-free ``ast`` implementation (already correct)
  — it stays the default and never needs the extra installed.
* **Every other supported language** uses a per-grammar ``tree-sitter`` query
  when its grammar wheel is importable, and otherwise returns ``None`` so the
  caller falls back to today's behaviour (no call edges for that language).

ONE entry point: :func:`extract_call_names`. ONE support probe:
:func:`supported_call_languages`. The per-language ``tree-sitter`` queries live
here AS DATA (:data:`_CALL_QUERIES`) so a new grammar is a one-row change.

Soft-fail contract (the sanctioned exception to loud-fail)
----------------------------------------------------------
``tree-sitter`` and its grammar wheels are an OPTIONAL extra
(``pyproject [project.optional-dependencies] codegraph-ts``). An air-gapped or
minimal install that never installed the extra keeps working: every grammar
import is guarded, an ``ImportError`` (or any grammar-load failure) is logged
**once** per language, and the language falls back to the pre-existing regex /
no-op behaviour. This is the ONE place the codebase's "loud-fail over silent
degrade" rule is deliberately inverted — matching the playwright optional-
dependency precedent — because the whole point of the extra is that installs
without it are supported.

Name semantics
--------------
For ``obj.method()`` the extracted name is ``method``; for ``a::b::c()`` it is
``c``; for ``foo()`` it is ``foo`` — i.e. the trailing identifier component of
the callee, matching the Python ``ast`` path (which records ``Attribute.attr``
/ ``Name.id``). Names are returned **order-preserving + de-duplicated**, again
matching the ``ast`` path. Builtins are NOT stripped for non-Python languages
(the Python-specific ``_BUILTINS`` denylist has no cross-language analogue);
the downstream ``_resolve_call_target`` step drops any name that resolves to no
known function anyway, so unresolved builtin-ish names simply produce no edge.
"""
from __future__ import annotations

import ast
import logging
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language scope (canonical IDs — MUST match analyze_code_graph's
# ``_LANGUAGE_DISPLAY_TO_CANONICAL`` values so a row's stored ``language`` maps
# straight through). Excluded by ruling: ``proto`` (no call construct),
# ``powershell`` (no first-class grammar — stays regex/no-op). ``svelte`` is
# handled by the caller extracting the <script> block and passing it as
# ``javascript``; this module never sees ``svelte``.
# ---------------------------------------------------------------------------

#: Python's implementation is the ``ast`` path below — dependency-free, always
#: available. It is listed here so :func:`supported_call_languages` always
#: reports it regardless of whether the tree-sitter extra is installed.
_PYTHON_LANG = "python"

# Per-language tree-sitter spec: canonical language id -> (
#     grammar module name,
#     factory attribute on that module (``language`` for all but typescript),
#     primary query source (captures @callee = the callee expression node),
#     optional secondary query source (e.g. rust macro invocations) or None,
# ).
#
# The primary query captures the *callee expression* (which may be a bare
# identifier, a scoped/selector/field expression, etc.); :func:`_leaf_name`
# then reduces it to the trailing identifier — so one query per grammar covers
# free calls, method calls and namespaced calls uniformly.
_CallSpec = Tuple[str, str, str, Optional[str]]
_CALL_QUERIES: Dict[str, _CallSpec] = {
    "rust": (
        "tree_sitter_rust", "language",
        "(call_expression function: (_) @callee)",
        "(macro_invocation macro: (identifier) @callee)",
    ),
    "go": (
        "tree_sitter_go", "language",
        "(call_expression function: (_) @callee)",
        None,
    ),
    "java": (
        "tree_sitter_java", "language",
        "(method_invocation name: (identifier) @callee)",
        None,
    ),
    "ruby": (
        "tree_sitter_ruby", "language",
        "(call method: (identifier) @callee)",
        None,
    ),
    "cpp": (
        "tree_sitter_cpp", "language",
        "(call_expression function: (_) @callee)",
        None,
    ),
    "csharp": (
        "tree_sitter_c_sharp", "language",
        "(invocation_expression function: (_) @callee)",
        None,
    ),
    "lua": (
        "tree_sitter_lua", "language",
        "(function_call name: (_) @callee)",
        None,
    ),
    "shell": (
        "tree_sitter_bash", "language",
        "(command name: (command_name) @callee)",
        None,
    ),
    "javascript": (
        "tree_sitter_javascript", "language",
        "(call_expression function: (_) @callee)",
        None,
    ),
    "typescript": (
        "tree_sitter_typescript", "language_typescript",
        "(call_expression function: (_) @callee)",
        None,
    ),
}

# Cache of compiled runtimes per language. Membership (``lang in _COMPILED``)
# distinguishes "not yet attempted" from an attempted load; a stored value of
# ``None`` means "tried and the grammar is not importable" (so we don't
# re-attempt + re-log on every call). Populated lazily under the lock.
_COMPILED: Dict[str, Optional["_LangRuntime"]] = {}
_COMPILE_LOCK = threading.Lock()


class _LangRuntime:
    """Holds the loaded tree-sitter ``Language`` + compiled queries for one
    language. Kept tiny + picklable-free; instances are cached process-wide."""

    __slots__ = ("language", "queries")

    def __init__(self, language, queries) -> None:  # noqa: ANN001 — ts types optional
        self.language = language
        self.queries = queries  # list of compiled Query objects


def _load_runtime(lang: str) -> Optional["_LangRuntime"]:
    """Import the grammar + compile the queries for ``lang`` once.

    Returns a :class:`_LangRuntime`, or ``None`` when the tree-sitter core or
    the grammar wheel is not importable (logged once). Thread-safe + cached.
    """
    if lang in _COMPILED:
        return _COMPILED[lang]
    with _COMPILE_LOCK:
        if lang in _COMPILED:
            return _COMPILED[lang]
        runtime = _try_load_runtime(lang)
        _COMPILED[lang] = runtime
        return runtime


def _try_load_runtime(lang: str) -> Optional["_LangRuntime"]:
    spec = _CALL_QUERIES.get(lang)
    if spec is None:
        return None
    mod_name, factory_attr, primary_q, secondary_q = spec
    try:
        import importlib

        from tree_sitter import Language, Query  # type: ignore

        grammar_mod = importlib.import_module(mod_name)
        language = Language(getattr(grammar_mod, factory_attr)())
        queries = [Query(language, primary_q)]
        if secondary_q:
            queries.append(Query(language, secondary_q))
        return _LangRuntime(language, queries)
    except Exception as exc:  # noqa: BLE001 — soft-fail: ImportError + any
        # grammar-load/ABI/query-compile error → fall back to no edges.
        logger.info(
            "codegraph_calls: tree-sitter call extraction unavailable for "
            "language '%s' (%s: %s); falling back to no call edges for this "
            "language. Install the optional extra to enable it: "
            "pip install '.[codegraph-ts]'",
            lang, type(exc).__name__, exc,
        )
        return None


# Identifier-leaf reducer: the trailing name component of a callee expression.
# A "name" leaf is one whose text is a bare identifier (starts alpha/underscore,
# remainder alnum/underscore). We take the LAST such leaf in source order,
# which is the called member for ``obj.method`` / ``a::b::c`` / ``pkg.Fn``.


def _leaf_name(node) -> Optional[str]:  # noqa: ANN001 — ts node type optional
    leaves: List = []

    def _collect(n) -> None:  # noqa: ANN001
        if n.child_count == 0:
            leaves.append(n)
        else:
            for child in n.children:
                _collect(child)

    _collect(node)
    for leaf in reversed(leaves):
        text = leaf.text.decode("utf-8", "replace")
        if text and (text[0].isalpha() or text[0] == "_") and _is_identifier(text):
            return text
    return None


def _is_identifier(text: str) -> bool:
    return all(ch.isalnum() or ch == "_" for ch in text)


# ---------------------------------------------------------------------------
# Python ``ast`` implementation — the existing (dependency-free) path, moved
# behind the facade unchanged in behaviour. Keeps the builtins denylist +
# order-preserving dedup byte-identical to the analyzer's former inline copy.
# ---------------------------------------------------------------------------

_PY_BUILTINS = frozenset({
    'print', 'len', 'range', 'enumerate', 'zip', 'map', 'filter',
    'sorted', 'reversed', 'list', 'dict', 'set', 'tuple', 'str',
    'int', 'float', 'bool', 'bytes', 'type', 'isinstance',
    'issubclass', 'hasattr', 'getattr', 'setattr', 'delattr',
    'super', 'property', 'staticmethod', 'classmethod',
    'open', 'iter', 'next', 'id', 'hash', 'repr', 'abs',
    'min', 'max', 'sum', 'any', 'all', 'ord', 'chr', 'hex',
    'vars', 'dir', 'format', 'input', 'round',
})


def _extract_python_calls(body: str) -> Optional[List[str]]:
    """Python call extraction via ``ast`` — the dependency-free default.

    Returns the ordered, de-duplicated list of called names (builtins
    stripped), or ``None`` when the body does not parse (caller then falls
    back exactly as the analyzer's ``except SyntaxError: continue`` did).
    """
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return None
    calls: List[str] = []
    seen: set = set()
    for child in ast.walk(tree):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name and name not in _PY_BUILTINS and name not in seen:
            seen.add(name)
            calls.append(name)
    return calls


def _extract_treesitter_calls(lang: str, body: str) -> Optional[List[str]]:
    runtime = _load_runtime(lang)
    if runtime is None:
        return None
    try:
        from tree_sitter import Parser, QueryCursor  # type: ignore

        parser = Parser(runtime.language)
        tree = parser.parse(body.encode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 — parse failure → no edges (soft)
        logger.info(
            "codegraph_calls: tree-sitter parse failed for language '%s' "
            "(%s); no call edges for this body.", lang, type(exc).__name__,
        )
        return None
    calls: List[str] = []
    seen: set = set()
    for query in runtime.queries:
        try:
            cursor = QueryCursor(query)
            captures = cursor.captures(tree.root_node)
        except Exception:  # noqa: BLE001 — query mismatch on a body → skip
            continue
        for node in captures.get("callee", []):
            name = _leaf_name(node)
            if name and name not in seen:
                seen.add(name)
                calls.append(name)
    return calls


def extract_call_names(language: str, body: str) -> Optional[List[str]]:
    """Extract the names of functions/methods called within ``body``.

    Args:
        language: canonical language id (``python``, ``rust``, ``go``, …) as
            stamped by ``analyze_code_graph._canonical_lang_id``. Unknown or
            unsupported ids return ``None``.
        body: the function/method source body.

    Returns:
        An ordered, de-duplicated ``list[str]`` of called names, OR ``None``
        when this language has no available extractor (grammar not installed,
        or language out of scope). ``None`` is the caller's signal to keep
        today's behaviour (no call edges for the row). An empty list ``[]`` is
        a *positive* result meaning "extracted, found no calls".
    """
    lang = (language or "").strip().lower()
    if not lang:
        return None
    if lang == _PYTHON_LANG:
        return _extract_python_calls(body)
    if lang not in _CALL_QUERIES:
        return None
    if not body:
        return []
    return _extract_treesitter_calls(lang, body)


def supported_call_languages() -> "frozenset[str]":
    """Return the set of canonical language ids for which call extraction is
    currently available in THIS process.

    Always includes ``python`` (dependency-free ``ast``). Includes each
    tree-sitter language whose grammar wheel is importable right now (probed
    lazily + cached). Used by the weaviate MCP to decide, per query, whether an
    empty ``callers`` / ``path`` result is genuinely "no callers" vs
    "unsupported for this language".
    """
    available = {_PYTHON_LANG}
    for lang in _CALL_QUERIES:
        if _load_runtime(lang) is not None:
            available.add(lang)
    return frozenset(available)
