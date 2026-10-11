# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-file Rust test modules for the code graph (v0.2.102 W6-G).

The analyzer extracts one file at a time, so a test module DECLARED in one
file and DEFINED in another —

    // src/lib.rs
    #[cfg(test)]
    mod tests;            // body lives in src/tests.rs

    #[cfg(test)]
    #[path = "testing/helpers.rs"]
    mod helpers;

— used to have its functions indexed as production code: nothing in
``src/tests.rs`` says it is test-only. This module is the per-crate pre-pass
that knows: it reads every Rust file's out-of-line ``mod x;`` declarations
(:func:`vco_lib.codegraph_defs.extract_rust_module_declarations`, the same
tree-sitter layer and the same test-only rule as the definitions walk),
resolves each to its file by Rust's module path rules (``x.rs`` /
``x/mod.rs``, mod-rs vs non-mod-rs directories, inline-module nesting,
``#[path]``), and answers three questions for the analyzer's walk:

* :func:`rust_file_is_test_only` — the Rust extractor drops every class and
  function of a test-only file (its module row stays, like a file whose own
  ``#![cfg(test)]`` gates all of it);
* :func:`rust_effective_file_hash` — the module hash the unchanged-file gate
  compares. It equals ``sha256(content)`` for every production file (no churn
  for existing graphs) and is salted for a test-only file, so a gate added or
  removed in ``lib.rs`` changes the stored hash of ``tests.rs`` and the gate
  re-walks it even though its own bytes did not change;
* :func:`rust_module_dependents` — the files whose test-only status a changed
  file can flip (its transitive module descendants), which ``--incremental``
  and the per-edit single-file scope add to the re-walk set.

Test-only: a file reached from a crate root only through at least one gated
declaration (or through a test-only file). A file nothing declares is a crate
root or an orphan and stays production; so does a declaration cycle no root
reaches. Without the Rust grammar there is no declaration data and nothing is
gated — the pre-v0.2.102 behaviour, never a guess.

Scope of one scan: the package directory (nearest ``Cargo.toml`` at or above
the file, bounded by the walk's repo root; the repo root itself when there is
none), excluding nested packages, ``target``, ``vendor``, ``node_modules`` and
dot-directories. The file listing is cached per process (one analyzer run);
each file's declarations are cached by ``(mtime_ns, size)``, so an edit within
the process is seen. :func:`clear_cache` resets both.
"""
from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from vco_lib.codegraph_defs import RustModDecl, extract_rust_module_declarations

#: Directories never part of a crate's module tree.
_EXCLUDED_DIRS = frozenset({"target", "vendor", "node_modules"})

#: Appended to a test-only file's content before hashing — any constant that
#: makes the stored hash differ from ``sha256(content)``.
_TEST_ONLY_HASH_SALT = "\0vco:rust-test-only-module"

#: File names whose child modules live in the file's OWN directory.
_MOD_RS_NAMES = frozenset({"lib.rs", "main.rs", "mod.rs", "build.rs"})
#: Package sub-directories whose ``*.rs`` files are crate roots (Cargo targets).
_ROOT_TARGET_DIRS = frozenset({"tests", "examples", "benches"})


@dataclass(frozen=True)
class _Graph:
    fingerprint: Tuple[Tuple[str, int, int], ...]
    children: Dict[Path, Tuple[Path, ...]]
    test_only: FrozenSet[Path]


_LOCK = threading.RLock()
_LISTINGS: Dict[Path, List[Path]] = {}
_DECLS: Dict[Path, Tuple[Tuple[int, int], Optional[List[RustModDecl]]]] = {}
_GRAPHS: Dict[Path, _Graph] = {}


def clear_cache() -> None:
    """Forget every listing, declaration and graph (tests; a long-lived host)."""
    with _LOCK:
        _LISTINGS.clear()
        _DECLS.clear()
        _GRAPHS.clear()


def _norm(path: Path) -> Path:
    return Path(os.path.normpath(str(path)))


def _package_root(file_path: Path, repo_root: Path) -> Optional[Path]:
    """Nearest directory at or above ``file_path`` holding ``Cargo.toml``,
    never above ``repo_root``; ``repo_root`` when none does; None when the
    file is not under ``repo_root`` (no scan scope can be trusted)."""
    file_path, repo_root = _norm(file_path), _norm(repo_root)
    try:
        file_path.relative_to(repo_root)
    except ValueError:
        return None
    current = file_path.parent
    while True:
        if (current / "Cargo.toml").is_file():
            return current
        if current == repo_root or current.parent == current:
            return repo_root
        current = current.parent


def _list_rust_files(pkg_root: Path) -> List[Path]:
    with _LOCK:
        cached = _LISTINGS.get(pkg_root)
    if cached is not None:
        return cached
    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(pkg_root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in _EXCLUDED_DIRS
            and not (here / d / "Cargo.toml").is_file()  # a nested package
        )
        found.extend(_norm(here / f) for f in sorted(filenames) if f.endswith(".rs"))
    with _LOCK:
        _LISTINGS[pkg_root] = found
    return found


def _stat_key(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _declarations(path: Path, key: Tuple[int, int]) -> Optional[List[RustModDecl]]:
    with _LOCK:
        cached = _DECLS.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        decls = None
    else:
        decls = extract_rust_module_declarations(text)
    with _LOCK:
        _DECLS[path] = (key, decls)
    return decls


def _is_mod_rs(path: Path, pkg_root: Path) -> bool:
    """Do ``path``'s child modules live in its own directory? True for
    ``lib.rs`` / ``main.rs`` / ``mod.rs`` / ``build.rs`` and for the Cargo
    target roots ``<pkg>/{tests,examples,benches}/*.rs``, ``<pkg>/src/bin/*.rs``."""
    if path.name in _MOD_RS_NAMES:
        return True
    parent = path.parent
    if parent.name in _ROOT_TARGET_DIRS and parent.parent == pkg_root:
        return True
    return parent.name == "bin" and parent.parent == pkg_root / "src"


def _strip_raw(name: str) -> str:
    return name[2:] if name.startswith("r#") else name


def resolve_declaration(declaring_file: Path, decl: RustModDecl, pkg_root: Path,
                        exists: Callable[[Path], bool]) -> Optional[Path]:
    """The file ``decl`` (declared in ``declaring_file``) loads, by the Rust
    reference's module path rules; None when no candidate exists."""
    declaring_file = _norm(declaring_file)
    if _is_mod_rs(declaring_file, pkg_root):
        directory = declaring_file.parent
    else:
        directory = declaring_file.parent / declaring_file.stem
    for name, path_attr in decl.inline:
        directory = directory / (path_attr if path_attr else _strip_raw(name))
    if decl.path_attr:
        base = declaring_file.parent if not decl.inline else directory
        candidates = [base / decl.path_attr]
    else:
        name = _strip_raw(decl.name)
        candidates = [directory / f"{name}.rs", directory / name / "mod.rs"]
    for candidate in candidates:
        candidate = _norm(candidate)
        if exists(candidate):
            return candidate
    return None


def _graph(pkg_root: Path) -> _Graph:
    files = _list_rust_files(pkg_root)
    keyed = [(f, _stat_key(f)) for f in files]
    fingerprint = tuple((str(f), k[0], k[1]) for f, k in keyed if k is not None)
    with _LOCK:
        cached = _GRAPHS.get(pkg_root)
    if cached is not None and cached.fingerprint == fingerprint:
        return cached
    present: Set[Path] = {f for f, k in keyed if k is not None}
    edges: Dict[Path, List[Tuple[Path, bool]]] = {}
    declared: Set[Path] = set()
    for f, key in keyed:
        if key is None:
            continue
        for decl in _declarations(f, key) or ():
            target = resolve_declaration(f, decl, pkg_root, present.__contains__)
            if target is None or target == f:
                continue
            edges.setdefault(f, []).append((target, decl.test_gated))
            declared.add(target)
    roots = [f for f in present if f not in declared]
    reached = _reach(roots, edges, ungated_only=False)
    production = _reach(roots, edges, ungated_only=True)
    graph = _Graph(
        fingerprint=fingerprint,
        children={p: tuple(dict.fromkeys(c for c, _ in cs)) for p, cs in edges.items()},
        test_only=frozenset(reached - production),
    )
    with _LOCK:
        _GRAPHS[pkg_root] = graph
    return graph


def _reach(roots: Iterable[Path], edges: Dict[Path, List[Tuple[Path, bool]]],
           *, ungated_only: bool) -> Set[Path]:
    seen: Set[Path] = set(roots)
    stack = list(seen)
    while stack:
        for child, gated in edges.get(stack.pop(), ()):
            if (ungated_only and gated) or child in seen:
                continue
            seen.add(child)
            stack.append(child)
    return seen


def rust_file_is_test_only(file_path: Path, repo_root: Path) -> bool:
    """True when every route from a crate root to ``file_path`` passes a
    test-only module declaration. False without grammar / scope (conservative:
    the file stays indexed as before)."""
    if file_path.suffix != ".rs":
        return False
    pkg_root = _package_root(file_path, repo_root)
    if pkg_root is None:
        return False
    return _norm(file_path) in _graph(pkg_root).test_only


def rust_effective_file_hash(content: str, file_path: Path, repo_root: Path) -> str:
    """The module ``file_hash`` for a Rust file: ``sha256(content)``, salted
    when the file is test-only (see the module docstring for why)."""
    if rust_file_is_test_only(file_path, repo_root):
        content = content + _TEST_ONLY_HASH_SALT
    return hashlib.sha256(content.encode()).hexdigest()


def rust_module_dependents(changed: Iterable[Path], repo_root: Path) -> List[Path]:
    """The transitive module descendants of the changed ``.rs`` files — every
    file whose test-only status an edit of ``changed`` can flip — excluding
    the changed files themselves. Paths are normalized; order is stable."""
    out: Dict[Path, None] = {}
    changed_norm = {_norm(c) for c in changed if c.suffix == ".rs"}
    for start in sorted(changed_norm):
        pkg_root = _package_root(start, repo_root)
        if pkg_root is None:
            continue
        children = _graph(pkg_root).children
        stack = [start]
        seen = {start}
        while stack:
            for child in children.get(stack.pop(), ()):
                if child in seen:
                    continue
                seen.add(child)
                stack.append(child)
                if child not in changed_norm:
                    out[child] = None
    return list(out)


def add_rust_module_dependents(selected: List[Path], candidates: List[Path],
                               repo_root: Path) -> List[Path]:
    """``selected`` (a re-walk set) plus every member of ``candidates`` (the
    discovered files) that :func:`rust_module_dependents` names for the
    selected Rust files. Candidate objects are returned as given."""
    if not any(p.suffix == ".rs" for p in selected):
        return selected
    by_norm = {_norm(c): c for c in candidates}
    have = {_norm(s) for s in selected}
    extra = [by_norm[d] for d in rust_module_dependents(selected, repo_root)
             if d in by_norm and d not in have]
    return selected + extra if extra else selected


def single_file_scope(file_path: Path, repo_root: Path,
                      excluded: Callable[[Path], bool]) -> List[Path]:
    """The analyzer's per-edit (``--only-file`` / drain) re-walk set for one
    file: the file itself, plus — for a Rust file — its module dependents the
    directory walk would also index (``excluded`` is the walk's own
    ignored-directory verdict)."""
    return [file_path, *(d for d in rust_module_dependents([file_path], repo_root)
                         if not excluded(d))]
