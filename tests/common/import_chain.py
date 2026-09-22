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
    "module_scope_imports",
    "resolve_vco_lib_module",
    "third_party_reachable_from",
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
