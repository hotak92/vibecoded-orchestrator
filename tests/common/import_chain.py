# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One home for "what does this code import, transitively, at module scope?"

Two gates ask that question about ``install.py`` — the ``--bootstrap`` probe
(module-scope chain only) and the pre-venv phase (module scope plus the
function-local imports reachable before the venv exists) — and they asked it
with two hand-copied AST walkers. The second copy was written by pasting the
first, and inherited a blind spot the first could afford and it could not: a
dotted target like ``vco_lib.embedding_providers.openai`` was resolved as the
file ``vco_lib/embedding_providers.openai.py``, which does not exist, so the
walk silently skipped it along with every third-party import underneath.

Static, not dynamic: nothing here imports the modules it inspects, which is the
whole point — the gates must run on a machine that HAS the dependencies and
still answer for one that does not.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

__all__ = [
    "REPO_ROOT",
    "CallImportIndex",
    "guarded_import_ranges",
    "module_scope_imports",
    "resolve_vco_lib_module",
    "third_party_reachable_from",
    "third_party_reachable_from_call",
    "requirements_import_roots",
]

REPO_ROOT = Path(__file__).resolve().parents[2]

#: PyPI distribution name -> the name you actually ``import``. Only the ones
#: that differ; anything absent maps to itself with ``-`` normalised to ``_``.
_DIST_TO_IMPORT = {
    "pyyaml": "yaml",
    "weaviate-client": "weaviate",
    "sentence-transformers": "sentence_transformers",
    "mcp": "mcp",
}


def requirements_import_roots(
    requirements: Path | None = None,
) -> frozenset[str]:
    """Import-root names for everything in ``requirements.txt``.

    Derived rather than hand-listed so a dependency added to the install can
    never quietly fall outside a gate that is supposed to cover "the packages
    the venv provides". A hand-maintained copy of this list covered 7 of 16.
    """
    path = requirements or (REPO_ROOT / "requirements.txt")
    roots: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        dist = re.split(r"[<>=!;\[ ]", line, maxsplit=1)[0].strip().lower()
        if not dist:
            continue
        roots.add(_DIST_TO_IMPORT.get(dist, dist.replace("-", "_")))
    return frozenset(roots)


def module_scope_imports(tree: ast.Module):
    """Yield only the import statements at a module's TOP level.

    Deliberately not a full walk: an import nested in a function or a
    ``try:`` body has different failure semantics, and conflating them is how
    a gate starts reporting things its caller cannot act on.
    """
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node


def resolve_vco_lib_module(dotted: str) -> Path | None:
    """Path of ``vco_lib.<dotted>``, as a module file or a package ``__init__``.

    ``dotted`` is the part AFTER ``vco_lib.`` — so ``embedding_providers.openai``
    resolves to ``vco_lib/embedding_providers/openai.py``. Returns ``None`` when
    the name is not a module in this repo (a symbol imported ``from vco_lib``,
    for instance), which callers treat as "nothing further to walk".
    """
    parts = dotted.split(".")
    as_module = REPO_ROOT.joinpath("vco_lib", *parts).with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = REPO_ROOT.joinpath("vco_lib", *parts, "__init__.py")
    if as_package.is_file():
        return as_package
    return None


def _targets_of(node: ast.Import | ast.ImportFrom, current: str) -> tuple[list[str], list[str]]:
    """Split one import node into (third-party roots, vco_lib dotted targets).

    ``current`` is the dotted name (below ``vco_lib.``) of the module being
    read, needed to resolve relative imports — ``from . import x`` inside
    ``vco_lib/embedding_providers/ollama.py`` means
    ``vco_lib.embedding_providers.x``.
    """
    external: list[str] = []
    internal: list[str] = []

    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "vco_lib" or alias.name.startswith("vco_lib."):
                if alias.name != "vco_lib":
                    internal.append(alias.name.split(".", 1)[1])
            else:
                external.append(alias.name.split(".")[0])
        return external, internal

    # ast.ImportFrom
    if node.level:
        # Relative: walk up `level - 1` packages from the current module's
        # package, then append the module part.
        pkg = current.split(".")[:-1]
        up = node.level - 1
        base = pkg[: len(pkg) - up] if up else pkg
        prefix = ".".join([*base, node.module] if node.module else base)
        if prefix:
            internal.append(prefix)
        # `from . import name` — each name may itself be a submodule.
        if not node.module:
            for alias in node.names:
                internal.append(".".join([*base, alias.name]))
        return external, internal

    module = node.module or ""
    if module == "vco_lib":
        internal += [alias.name for alias in node.names]
    elif module.startswith("vco_lib."):
        internal.append(module.split(".", 1)[1])
    elif module:
        external.append(module.split(".")[0])
    return external, internal


def third_party_reachable_from(
    start: str | Path,
    *,
    is_install_py: bool = False,
) -> dict[str, str]:
    """Third-party package -> the ``vco_lib`` module that imports it.

    Walks module-scope imports transitively through ``vco_lib``. ``start`` is
    either a dotted name below ``vco_lib.`` or a path (for ``install.py``).
    Standard-library names are ignored; so is ``vco_lib`` itself.

    Returns a mapping rather than a set so a failure message can name WHERE
    the dependency enters, which is the thing a reader needs in order to fix
    it.
    """
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, str] = {}
    seen: set[str] = set()

    if isinstance(start, Path):
        pending: list[tuple[str, Path]] = [("install.py" if is_install_py else start.name, start)]
    else:
        resolved = resolve_vco_lib_module(start)
        pending = [(start, resolved)] if resolved else []

    while pending:
        name, path = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in module_scope_imports(tree):
            external, internal = _targets_of(node, name)
            for root in external:
                if root and root not in stdlib and root != "vco_lib":
                    found.setdefault(root, name)
            for dotted in internal:
                child = resolve_vco_lib_module(dotted)
                if child is not None and dotted not in seen:
                    pending.append((dotted, child))
    return found


# ─── interprocedural walk: what executes when a SYMBOL is called ─────────
#
# v0.2.97: the walkers above are MODULE-SCOPE-only, which is exactly right
# for their question ("does IMPORTING this module need the package?") and
# exactly blind to the failure install-smoke shipped that cycle: install.py
# imported ``vco_lib.openai_key`` (clean module scope) and called
# ``migrate_dotenv_openai_key``, whose BODY did ``from vco_lib import
# agent_secrets`` — and agent_secrets' MODULE scope imports
# ``vco_lib.project_config``, which imports ``requests`` unguarded. The
# module-scope walk never descends into a called symbol's body, so the gate
# was green while every fresh install crashed at step 9.
#
# The machinery below answers the sharper question the post-venv gate needs:
# "when THIS callable runs, which third-party imports actually execute?" —
# following the called symbol's body, the same-module functions it calls, and
# the vco_lib symbols its imports bind.


def _handler_catches_imports(handler: ast.ExceptHandler) -> bool:
    """Does this except-clause absorb an ImportError?

    ``ImportError``/``ModuleNotFoundError`` name it; ``Exception``/bare
    covers it; anything else (``OSError`` alone, ``RuntimeError``…) does not.
    """
    if handler.type is None:
        return True
    names: list[str] = []
    node = handler.type
    if isinstance(node, ast.Name):
        names = [node.id]
    elif isinstance(node, ast.Tuple):
        names = [e.id for e in node.elts if isinstance(e, ast.Name)]
    return any(
        n in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException")
        for n in names
    )


def guarded_import_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    """``(first_line, last_line)`` of every statement whose imports sit in a
    ``try`` an ImportError-catching handler can absorb.

    An import on one of these lines is GUARDED — the callable survives the
    package being absent, which is the post-venv rule's "working fallback"
    criterion. (What the handler does next is the callable's business; the
    pre-venv gate remains the one that forbids the import outright.)"""
    out: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_handler_catches_imports(h) for h in node.handlers):
            for stmt in node.body:
                out.append((stmt.lineno, stmt.end_lineno or stmt.lineno))
    return out


def _attr_names_on(root: ast.AST, base: str) -> set[str]:
    """Attribute names accessed on the NAME ``base`` anywhere under *root*."""
    return {
        n.attr
        for n in ast.walk(root)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == base
    }


class CallImportIndex:
    """One Python file's toplevel-def index for interprocedural import walks.

    ``third_party_from_call(symbol)`` walks a callable (or the module scope,
    with ``None``) and reports third-party packages that would EXECUTE when
    it runs:

    * the module scope of every ``vco_lib`` module the chain imports
      (importing runs it) — but NOT the entry file's own module scope, which
      ran at import time, long before the callable, and is the
      bootstrap/pre-venv gates' question;
    * the bodies of the symbols actually called — resolved through
      ``from vco_lib.X import f`` bindings, ``from vco_lib import X`` +
      ``X.attr`` uses, and same-module calls by name — transitively.

    Known limits, deliberate: calls on RETURNED objects and injected
    callables are not followed (nothing in install.py's flow imports through
    them); an import wrapped in a try whose handler catches but then
    re-raises still counts as guarded — the runtime behaviour of each
    guarded site is pinned by behavioural tests, not by this static walk.
    """

    def __init__(self, path: Path, *, name: str) -> None:
        self.path = path
        self.name = name  # how violations name this file
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.toplevel: dict[str, ast.AST] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.toplevel[node.name] = node
        self.guarded = guarded_import_ranges(self.tree)

    def third_party_from_call(self, symbol: str | None) -> dict[str, str]:
        """Third-party package -> where it executes, for a call of *symbol*.

        Guarded imports (see :func:`guarded_import_ranges`) are NOT reported:
        they are the sanctioned post-venv fallback shape."""
        found: dict[str, str] = {}
        seen: set[tuple[str, str | None]] = set()
        # (module, symbol, reached_by_import) — the last flag says the MODULE
        # SCOPE executes too (the import that bound it ran inside the walk).
        pending: list[tuple[str, str | None, bool]] = [(self.name, symbol, False)]
        while pending:
            mod_name, sym, reached_by_import = pending.pop()
            if (mod_name, sym) in seen:
                continue
            seen.add((mod_name, sym))
            index = _index_for(mod_name)
            if index is None:
                continue
            if reached_by_import or sym is None:
                for node in module_scope_imports(index.tree):
                    external, internal = _targets_of(node, mod_name)
                    for root in external:
                        if root and root not in sys.stdlib_module_names and root != "vco_lib":
                            found.setdefault(
                                root, f"vco_lib.{mod_name} imports it at module scope"
                            )
                    for dotted in internal:
                        pending.append((dotted, None, True))
            if sym is None:
                continue
            node = index.toplevel.get(sym)
            if node is None:
                index._follow_reexport(sym, pending)
                continue
            bodies: list[ast.AST] = [node]
            if isinstance(node, ast.ClassDef):
                bodies += [m for m in node.body if isinstance(m, ast.FunctionDef)]
            for body in bodies:
                index._walk_body(mod_name, body, found, pending)

        return found

    def _follow_reexport(
        self, sym: str, pending: list[tuple[str, str | None, bool]]
    ) -> None:
        """``from vco_lib.X import sym`` at module scope: follow it there."""
        for node in module_scope_imports(self.tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module == "vco_lib":
                for alias in node.names:
                    if (alias.asname or alias.name) == sym:
                        pending.append((alias.name, None, True))
            elif node.module.startswith("vco_lib."):
                for alias in node.names:
                    if (alias.asname or alias.name) == sym:
                        pending.append((node.module.split(".", 1)[1], sym, True))

    def _walk_body(
        self,
        mod_name: str,
        body: ast.AST,
        found: dict[str, str],
        pending: list[tuple[str, str | None, bool]],
    ) -> None:
        def _is_guarded(node: ast.stmt) -> bool:
            return any(lo <= node.lineno <= hi for lo, hi in self.guarded)

        for sub in ast.walk(body):
            if isinstance(sub, ast.ImportFrom):
                if _is_guarded(sub):
                    continue  # the failure is absorbed: nothing executes
                module = sub.module or ""
                if sub.level:  # relative import inside a vco_lib package
                    _external, internal = _targets_of(sub, mod_name)
                    for dotted in internal:
                        pending.append((dotted, None, True))
                    continue
                if module == "vco_lib":
                    for alias in sub.names:
                        attrs = _attr_names_on(body, alias.asname or alias.name)
                        for attr in sorted(attrs) or [None]:
                            pending.append((alias.name, attr, True))
                elif module.startswith("vco_lib."):
                    for alias in sub.names:
                        pending.append((module.split(".", 1)[1], alias.asname or alias.name, True))
                else:
                    self._note_external(sub, module.split(".")[0], found)
            elif isinstance(sub, ast.Import):
                if _is_guarded(sub):
                    continue
                for alias in sub.names:
                    if alias.name.startswith("vco_lib."):
                        alias_name = alias.asname or alias.name.split(".", 1)[1]
                        attrs = _attr_names_on(body, alias_name)
                        for attr in sorted(attrs) or [None]:
                            pending.append((alias.name.split(".", 1)[1], attr, True))
                    else:
                        self._note_external(sub, alias.name.split(".")[0], found)
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                target = self.toplevel.get(sub.func.id)
                if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    pending.append((mod_name, sub.func.id, False))

    def _note_external(self, node: ast.stmt, root: str, found: dict[str, str]) -> None:
        if not root or root in sys.stdlib_module_names:
            return
        if any(lo <= node.lineno <= hi for lo, hi in self.guarded):
            return  # guarded: the sanctioned fallback shape
        found.setdefault(root, f"{self.name} imports it in a function body, unguarded")


_INDEX_CACHE: dict[str, CallImportIndex | None] = {}


def _index_for(mod_name: str) -> CallImportIndex | None:
    """The :class:`CallImportIndex` for ``install.py`` or a ``vco_lib``
    submodule (cached per session — the gates read source, never write it).

    ``mod_name`` may carry a leading ``vco_lib.`` (an index's ``name`` is
    prefixed for its violation messages); it is normalised here so the seen
    set stays consistent."""
    if mod_name.startswith("vco_lib."):
        mod_name = mod_name.split(".", 1)[1]
    if mod_name == "install.py":
        if "install.py" not in _INDEX_CACHE:
            _INDEX_CACHE["install.py"] = CallImportIndex(
                REPO_ROOT / "install.py", name="install.py"
            )
        return _INDEX_CACHE["install.py"]
    path = resolve_vco_lib_module(mod_name)
    if path is None:
        return None
    if mod_name not in _INDEX_CACHE:
        _INDEX_CACHE[mod_name] = CallImportIndex(path, name=f"vco_lib.{mod_name}")
    return _INDEX_CACHE[mod_name]


def third_party_reachable_from_call(module_dotted: str, symbol: str | None) -> dict[str, str]:
    """Single-shot form of :class:`CallImportIndex` for a ``vco_lib``
    module's symbol — the walker's own regression tests use it."""
    index = _index_for(module_dotted)
    if index is None:
        return {}
    return index.third_party_from_call(symbol)
