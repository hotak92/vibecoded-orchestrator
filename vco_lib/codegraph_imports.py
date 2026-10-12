# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Language-aware import → module resolution for the code graph (v0.2.102 W6-O).

The ONE place a stored ``import_names`` entry becomes a CodeModule ``imports``
edge. ``analyze_code_graph.create_cross_references`` builds an
:class:`ImportIndex` over every known module path and asks
:func:`resolve_import_targets` for each (importer, import name) pair; the
MCP's and the query CLI's ``imports`` readers use
:func:`import_edge_compatible` / :func:`filter_compatible_import_rows` to hide
edges that the resolver below can no longer produce, and
:func:`purge_incompatible_import_edges` deletes the stored ones on every walk.

Why this module exists (the measured defects it closes)
-------------------------------------------------------
Until W6-O the resolver was ONE stem lookup for every language: an import name
was looked up as written, then by its last dotted component, against an index
of every module's file stem — whatever the language of either side. On this
repository that made 17 % of the stored Python ``imports`` edges point at Rust
and shell files (``model_router/usage.py`` "imported" a Rust ``logging.rs``),
while Rust (``crate::a::b``) and TypeScript (``./x``, ``$lib/x``) names, which
never look like a stem, resolved almost never.

Resolution rules — an import only resolves to a module of a COMPATIBLE language
(:func:`import_family`; the language of a path comes from the analyzer's one
extension table, mirrored and parity-pinned in
``vco_lib.inject_intent.EXT_TO_LANG``):

* **Python** — the pre-W6-O rule, restricted to Python modules: the dotted
  name as written (indexed by file stem and by the last two path segments),
  then its last component. ``.pyi`` stubs are not walked by the analyzer, so
  they never are module rows to resolve to.
* **Rust** — ``use`` trees (``crate::a::b``, ``self::…``, ``super::…`` any
  number of times, ``{a, b::{c, self}}`` groups, ``as`` renames, ``*`` globs),
  workspace crates by the name their ``Cargo.toml`` declares (``[lib] name``,
  else ``[package] name``, ``-`` read as ``_``; read once when the index is
  built with a ``repo_root``, the package directory name when it cannot be
  read), a bare first segment naming a child module
  of the importer, and out-of-line ``mod x;`` declarations (stored as
  ``"mod x"``). A path resolves to the longest prefix that is a module file
  (``x.rs`` or ``x/mod.rs``); the trailing item names (types, functions) are
  dropped. Not supported: ``#[path]`` attributes and paths relative to an
  INLINE ``mod x { … }`` block; sysroot (``std``, ``core``, ``alloc``,
  ``proc_macro``, ``test``) and external crates never resolve, even when a
  workspace crate shares the name.
* **TypeScript / JavaScript / Svelte** (one family) — relative specifiers
  (``./x``, ``../x``), the SvelteKit ``$lib/`` alias (the nearest ancestor
  ``src/lib``) and the ``@/`` / ``~/`` aliases (the ``src/`` of the nearest
  ancestor that has one, else the repository root — never the importer's own
  folder). Each tries the path as written, ``.js``/``.mjs``/
  ``.cjs``/``.jsx`` → ``.ts``/``.tsx`` (ESM-style TypeScript), then appended
  ``.ts .tsx .js .jsx .mjs .svelte`` and ``/index.*``. ``tsconfig.json``
  ``paths`` are NOT read (the index is built from stored module paths, with
  no file access); bare package specifiers never resolve.
* **Shell** — ``source``/``.`` arguments. A literal relative path resolves
  against the importer's directory, then the repository root, and nowhere
  else; only a path with a dynamic prefix (``"$DIR/_lib/x.sh"``,
  ``"$(dirname "$0")/x.sh"``) resolves by its literal tail, preferring the
  candidate closest to the importer.
* **PowerShell** — a dot-sourced path takes the shell path rule (``\\`` read as
  ``/``); a bare ``Import-Module Name`` takes the generic stem rule.
* **Every other language** — the generic stem rule, restricted to that
  language.

Pure resolution: no Weaviate, and no I/O beyond the ``Cargo.toml`` reads of
:func:`build_import_index` when it is given a ``repo_root``. Paths are
repository-relative POSIX strings.

Stored wrong edges (v0.2.102 W6-O review HIGH-1, owner decision 2026-10-12)
--------------------------------------------------------------------------
Hiding a pre-W6-O wrong-language edge on read is not enough: the reverse
``imports`` query is capped, so hidden rows still used up the cap. Every
analyzer walk therefore DELETES, one beacon at a time, each stored ``imports``
edge whose two endpoints have known, incompatible languages
(:func:`purge_incompatible_import_edges`). The decision needs only the two
endpoints — never a desired edge set — so a partial or soft-failed scan can
only miss edges, never delete a right one; an edge whose either side is
unknown is left alone, and a module's ref set is never replaced. Running on
every walk (not one release rung) means an install that skips a release, or
whose re-walk soft-failed, still converges on its next ordinary walk.
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from vco_lib.inject_intent import language_for_path

__all__ = [
    "ImportIndex",
    "build_import_index",
    "resolve_import_targets",
    "import_family",
    "import_edge_compatible",
    "row_language",
    "filter_compatible_import_rows",
    "expand_rust_use_tree",
    "incompatible_import_edges",
    "purge_incompatible_import_edges",
    "DELETE_INCOMPATIBLE_IMPORT_EDGES",
]

logger = logging.getLogger(__name__)

#: Kill switch for :func:`purge_incompatible_import_edges` (owner decision
#: 2026-10-12: deletion ON). Read at call time, so a test or an emergency
#: patch can flip it; ``False`` restores hide-on-read only.
DELETE_INCOMPATIBLE_IMPORT_EDGES = True

#: Languages that import each other. Everything else is its own family.
_FAMILY: Dict[str, str] = {
    "typescript": "js",
    "javascript": "js",
    "svelte": "js",
}

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".svelte")
_JS_RUNTIME_EXTS = (".js", ".mjs", ".cjs", ".jsx")
_RUST_MOD_RS = frozenset({"lib.rs", "main.rs", "mod.rs"})
_RUST_ROOT_FILES = ("lib.rs", "main.rs")
#: Sysroot crates: ``use core::mem`` never means a workspace crate named core.
_RUST_SYSROOT_CRATES = frozenset({"std", "core", "alloc", "proc_macro", "test"})
#: Characters that make a shell path segment dynamic (not literally matchable).
_SHELL_DYNAMIC = re.compile(r"[$`(){}~*?\[\]]")


def import_family(language: Optional[str]) -> str:
    """The import family of a canonical language id (``""`` when unknown)."""
    lang = str(language or "").strip().lower()
    return _FAMILY.get(lang, lang)


def import_edge_compatible(src_language: Optional[str], dst_language: Optional[str]) -> bool:
    """Can a module of ``src_language`` import one of ``dst_language``?

    An unknown language on either side reads as compatible: a reader that
    cannot positively show an edge is wrong keeps it.
    """
    src, dst = import_family(src_language), import_family(dst_language)
    if not src or not dst:
        return True
    return src == dst


def row_language(props: Optional[Mapping[str, Any]]) -> str:
    """A CodeModule row's language: the stored ``language`` property, else the
    one its ``path`` extension implies."""
    props = props or {}
    lang = str(props.get("language") or "").strip().lower()
    if lang:
        return lang
    return language_for_path(str(props.get("path") or ""))


def filter_compatible_import_rows(source_props: Optional[Mapping[str, Any]],
                                  targets: Sequence[Any]) -> List[Any]:
    """Drop ``imports`` targets of a language the source cannot import.

    The read-side backstop of :func:`purge_incompatible_import_edges`: an edge
    stored by a pre-W6-O analyzer (a Python module "importing" ``logging.rs``)
    survives until the next walk deletes it, and a peer collection this
    install never walks keeps its edges — so every ``imports`` reader filters
    them with the same family rule.
    """
    src_lang = row_language(source_props)
    out: List[Any] = []
    for target in targets:
        props = getattr(target, "properties", None) or {}
        if import_edge_compatible(src_lang, row_language(props)):
            out.append(target)
    return out


@dataclass
class ImportIndex:
    """Every known module path, grouped the ways the resolvers look them up."""

    paths: FrozenSet[str] = frozenset()
    language: Dict[str, str] = field(default_factory=dict)
    #: family → import name → [paths] (stem and last-two-segment dotted keys).
    names: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    #: family → file basename → [paths] (path-suffix lookups).
    basenames: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    #: every ancestor directory of a known path ("" = repository root).
    dirs: FrozenSet[str] = frozenset()
    #: Rust workspace crate name → that crate's ``src`` directory.
    rust_crates: Dict[str, str] = field(default_factory=dict)


def _cargo_crate_name(repo_root: Optional[str], crate_dir: str) -> Optional[str]:
    """The crate name ``<repo_root>/<crate_dir>/Cargo.toml`` declares —
    ``[lib] name``, else ``[package] name`` — with ``-`` read as ``_``.
    ``None`` when there is no root, no file, or nothing readable."""
    if not repo_root:
        return None
    manifest = os.path.join(repo_root, *(crate_dir.split("/") if crate_dir else []), "Cargo.toml")
    try:
        with open(manifest, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return None
    for table in ("lib", "package"):
        section = data.get(table)
        name = section.get("name") if isinstance(section, dict) else None
        if isinstance(name, str) and name.strip():
            return name.strip().replace("-", "_")
    return None


def build_import_index(paths: Iterable[str], *, repo_root: Optional[str] = None) -> ImportIndex:
    """Index ``paths`` (repo-relative POSIX module paths) for resolution.

    ``repo_root`` (the directory those paths are relative to) lets each Rust
    library crate be indexed by the name its ``Cargo.toml`` declares, read
    once here; without it, or when the manifest cannot be read, a crate falls
    back to its package directory name. Sysroot names are never indexed.
    """
    ordered: List[str] = []
    seen: Set[str] = set()
    for raw in paths:
        p = str(raw or "").replace("\\", "/")
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    language: Dict[str, str] = {}
    names: Dict[str, Dict[str, List[str]]] = {}
    basenames: Dict[str, Dict[str, List[str]]] = {}
    dirs: Set[str] = {""}
    rust_crates: Dict[str, str] = {}
    for p in ordered:
        lang = language_for_path(p)
        language[p] = lang
        fam = import_family(lang)
        stem_parts = posixpath.splitext(p)[0].split("/")
        fam_names = names.setdefault(fam, {})
        fam_names.setdefault(stem_parts[-1], []).append(p)
        if len(stem_parts) > 1:
            fam_names.setdefault(".".join(stem_parts[-2:]), []).append(p)
        basenames.setdefault(fam, {}).setdefault(posixpath.basename(p), []).append(p)
        d = posixpath.dirname(p)
        while d and d not in dirs:
            dirs.add(d)
            d = posixpath.dirname(d)
        if lang == "rust" and (p == "src/lib.rs" or p.endswith("/src/lib.rs")):
            src_dir = posixpath.dirname(p)
            crate_dir = posixpath.dirname(src_dir)
            crate = _cargo_crate_name(repo_root, crate_dir)
            if crate is None:
                crate = posixpath.basename(crate_dir).replace("-", "_")
            if crate and crate not in _RUST_SYSROOT_CRATES:
                rust_crates.setdefault(crate, src_dir)
    return ImportIndex(
        paths=frozenset(ordered), language=language, names=names,
        basenames=basenames, dirs=frozenset(dirs), rust_crates=rust_crates,
    )


def resolve_import_targets(importer: str, name: str, index: ImportIndex) -> List[str]:
    """Module paths ``name`` (one stored import name of ``importer``) points at.

    Every returned path is a module of a language ``importer`` can import, and
    never ``importer`` itself. ``[]`` when nothing resolves. A Rust ``use``
    group may name several modules, hence a list.
    """
    importer = str(importer or "").replace("\\", "/")
    name = str(name or "").strip()
    if not importer or not name:
        return []
    lang = index.language.get(importer) or language_for_path(importer)
    fam = import_family(lang)
    if not fam:
        return []
    if fam == "rust":
        found = _resolve_rust(importer, name, index)
    elif fam == "js":
        found = _resolve_js(importer, name, index)
    elif fam == "shell":
        found = _resolve_path_like(importer, name, index, "shell")
    elif fam == "powershell" and re.search(r"[/\\]|\.ps(?:m?1)$", name, re.IGNORECASE):
        found = _resolve_path_like(importer, name.replace("\\", "/"), index, "powershell")
    else:
        found = _resolve_generic(name, index, fam)
    out: List[str] = []
    for p in found:
        if p and p != importer and p not in out:
            out.append(p)
    return out


# ── generic (Python and every language without its own rule) ────────────────


def _resolve_generic(name: str, index: ImportIndex, fam: str) -> List[str]:
    """The pre-W6-O rule, within one family: the name as written, then its
    last dotted component; first candidate wins."""
    fam_names = index.names.get(fam, {})
    candidates = fam_names.get(name) or fam_names.get(name.rsplit(".", 1)[-1]) or []
    return [candidates[0]] if candidates else []


# ── Rust ─────────────────────────────────────────────────────────────────────


def _split_top(text: str, sep: str = ",") -> List[str]:
    """Split ``text`` on ``sep`` at brace depth 0."""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def expand_rust_use_tree(tree: str) -> List[List[str]]:
    """``a::{b, c::{d, self}} `` → ``[[a, b], [a, c, d], [a, c]]``.

    ``as`` renames are dropped, a ``*`` glob ends its path, a leading ``::``
    is ignored. Malformed text yields what parsed cleanly (never raises).
    """
    text = " ".join(str(tree or "").split())
    out: List[List[str]] = []

    def walk(prefix: List[str], node: str, depth: int) -> None:
        node = node.strip()
        if not node or depth > 16:
            return
        brace = node.find("{")
        if brace >= 0 and node.endswith("}"):
            head = node[:brace].strip().rstrip(":").strip()
            segs = prefix + [s.strip() for s in head.split("::") if s.strip()]
            for part in _split_top(node[brace + 1:-1]):
                walk(segs, part, depth + 1)
            return
        node = re.split(r"\s+as\s+", node)[0].strip()
        segs = [s.strip() for s in node.split("::") if s.strip()]
        if segs and segs[-1] == "*":
            segs = segs[:-1]
        if segs and segs[-1] == "self":
            segs = segs[:-1]
        full = prefix + segs
        if full and all(re.fullmatch(r"r#\w+|\w+", s) for s in full):
            out.append([s[2:] if s.startswith("r#") else s for s in full])

    walk([], text.lstrip(":").strip(), 0)
    return out


def _rust_child_dir(module_file: str) -> str:
    """The directory holding ``module_file``'s child modules."""
    base = posixpath.basename(module_file)
    parent = posixpath.dirname(module_file)
    if base in _RUST_MOD_RS:
        return parent
    return posixpath.join(parent, posixpath.splitext(base)[0]) if parent else posixpath.splitext(base)[0]


def _rust_is_crate_dir(child_dir: str, index: ImportIndex) -> bool:
    return any(posixpath.join(child_dir, f) in index.paths for f in _RUST_ROOT_FILES)


def _rust_crate_dir(importer: str, index: ImportIndex) -> Optional[str]:
    d = posixpath.dirname(importer)
    while True:
        if _rust_is_crate_dir(d, index):
            return d
        if not d:
            return None
        d = posixpath.dirname(d)


def _rust_module_file(child_dir: str, index: ImportIndex) -> Optional[str]:
    """The file of the module whose children live in ``child_dir``."""
    for f in _RUST_ROOT_FILES:
        cand = posixpath.join(child_dir, f)
        if cand in index.paths:
            return cand
    for cand in (child_dir + ".rs", posixpath.join(child_dir, "mod.rs")):
        if cand in index.paths:
            return cand
    return None


def _rust_child(child_dir: str, seg: str, index: ImportIndex) -> Optional[str]:
    base = posixpath.join(child_dir, seg) if child_dir else seg
    for cand in (base + ".rs", posixpath.join(base, "mod.rs")):
        if cand in index.paths:
            return cand
    return None


def _rust_walk(child_dir: str, rest: Sequence[str], index: ImportIndex) -> Optional[str]:
    """Longest prefix of ``rest`` that is a module under ``child_dir``; the
    module of ``child_dir`` itself when none (or ``rest`` is empty)."""
    found: Optional[str] = None
    cur = child_dir
    for seg in rest:
        hit = _rust_child(cur, seg, index)
        if hit is None:
            break
        found = hit
        cur = _rust_child_dir(hit)
    return found or _rust_module_file(child_dir, index)


def _resolve_rust(importer: str, name: str, index: ImportIndex) -> List[str]:
    m = re.fullmatch(r"mod\s+(?:r#)?(\w+)", name)
    if m:
        hit = _rust_child(_rust_child_dir(importer), m.group(1), index)
        return [hit] if hit else []
    out: List[str] = []
    for segs in expand_rust_use_tree(name):
        hit = _resolve_rust_path(importer, segs, index)
        if hit:
            out.append(hit)
    return out


def _resolve_rust_path(importer: str, segs: List[str], index: ImportIndex) -> Optional[str]:
    if not segs:
        return None
    head = segs[0]
    if head == "crate":
        root = _rust_crate_dir(importer, index)
        return _rust_walk(root, segs[1:], index) if root is not None else None
    if head in ("self", "super"):
        child_dir = _rust_child_dir(importer)
        rest = list(segs)
        if rest[0] == "self":
            rest = rest[1:]
        while rest and rest[0] == "super":
            if _rust_is_crate_dir(child_dir, index):
                return None  # `super` of a crate root does not exist
            child_dir = posixpath.dirname(child_dir)
            rest = rest[1:]
        return _rust_walk(child_dir, rest, index)
    if head in index.rust_crates:
        return _rust_walk(index.rust_crates[head], segs[1:], index)
    # 2018 edition: a bare first segment may name a child module in scope.
    child_dir = _rust_child_dir(importer)
    if _rust_child(child_dir, head, index):
        return _rust_walk(child_dir, segs, index)
    return None


# ── TypeScript / JavaScript / Svelte ─────────────────────────────────────────


def _js_candidates(base: str) -> List[str]:
    cands = [base]
    stem, ext = posixpath.splitext(base)
    if ext in _JS_RUNTIME_EXTS:
        cands += [stem + ".ts", stem + ".tsx"]
    cands += [base + e for e in _JS_EXTS]
    cands += [posixpath.join(base, "index" + e) for e in _JS_EXTS]
    return cands


def _js_lookup(base: str, index: ImportIndex) -> Optional[str]:
    if not base or base.startswith(".."):
        return None
    for cand in _js_candidates(base):
        if cand in index.paths and import_family(index.language.get(cand)) == "js":
            return cand
    return None


def _ancestors(path: str) -> List[str]:
    out: List[str] = []
    d = posixpath.dirname(path)
    while True:
        out.append(d)
        if not d:
            return out
        d = posixpath.dirname(d)


def _resolve_js(importer: str, spec: str, index: ImportIndex) -> List[str]:
    if spec in (".", "..") or spec.startswith(("./", "../")):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
        hit = _js_lookup(base, index)
        return [hit] if hit else []
    roots: List[str] = []
    rest = ""
    if spec == "$lib" or spec.startswith("$lib/"):
        rest = spec[len("$lib/"):] if spec.startswith("$lib/") else ""
        for anc in _ancestors(importer):
            lib = posixpath.join(anc, "src", "lib") if anc else "src/lib"
            if lib in index.dirs:
                roots.append(lib)
                break
    elif spec.startswith(("@/", "~/")):
        # The alias root is ONE directory: the `src/` of the nearest ancestor
        # that has one, else the repository root. Probing every ancestor would
        # answer with the nearest folder that happens to hold a same-named file
        # (`@/utils` from `src/components/Card.ts` → `src/components/utils.ts`).
        rest = spec[2:]
        root = ""
        for anc in _ancestors(importer):
            src = posixpath.join(anc, "src") if anc else "src"
            if src in index.dirs:
                root = src
                break
        roots.append(root)
    for root in roots:
        base = posixpath.normpath(posixpath.join(root, rest)) if rest else root
        hit = _js_lookup(base, index)
        if hit:
            return [hit]
    return []


# ── shell / PowerShell path-like imports ─────────────────────────────────────


def _common_prefix_len(a: str, b: str) -> int:
    pa, pb = a.split("/"), b.split("/")
    n = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        n += 1
    return n


def _resolve_path_like(importer: str, arg: str, index: ImportIndex, fam: str) -> List[str]:
    arg = arg.replace('"', "").replace("'", "").strip()
    if not arg:
        return []
    segs = arg.split("/")
    dynamic_at = max((i for i, s in enumerate(segs) if _SHELL_DYNAMIC.search(s)), default=-1)

    def ok(p: str) -> bool:
        return p in index.paths and import_family(index.language.get(p)) == fam

    if dynamic_at < 0:
        if arg.startswith("/"):
            return []
        for base in (posixpath.dirname(importer), ""):
            cand = posixpath.normpath(posixpath.join(base, arg)) if base else posixpath.normpath(arg)
            if not cand.startswith("..") and ok(cand):
                return [cand]
        # A literal path names ONE file; when it is not where the shell would
        # look, an unrelated same-named file elsewhere is not the import.
        return []
    tail = segs[dynamic_at + 1:]
    tail = [s for s in tail if s]
    while tail and tail[0] in (".", ".."):
        tail = tail[1:]
    if not tail or ".." in tail:
        return []
    suffix = "/".join(tail)
    cands = [p for p in index.basenames.get(fam, {}).get(tail[-1], [])
             if (p == suffix or p.endswith("/" + suffix)) and ok(p)]
    if not cands:
        return []
    importer_dir = posixpath.dirname(importer)
    best = max(cands, key=lambda p: (_common_prefix_len(importer_dir, posixpath.dirname(p)), -len(p)))
    return [best]


# ── stored wrong-language edges (deleted on every walk) ──────────────────────


def _path_of_key(key: Any) -> str:
    """A module-cache key is ``(project_source, path)`` or a bare path."""
    if isinstance(key, tuple) and len(key) == 2:
        key = key[1]
    return str(key or "").replace("\\", "/")


def incompatible_import_edges(stored_refs: Mapping[str, Sequence[Any]],
                              path_by_uuid: Mapping[str, str]) -> List[Tuple[str, str]]:
    """Distinct ``(source_uuid, target_uuid)`` ``imports`` edges that are
    provably wrong: both endpoints are known modules whose languages are
    known and in different import families.

    An endpoint missing from ``path_by_uuid`` (a row this scan did not see, a
    target in another collection) or whose language is unknown leaves the
    edge alone — compatibility cannot be decided, so it is kept.
    """
    out: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for src_uuid, targets in stored_refs.items():
        src_fam = import_family(language_for_path(path_by_uuid.get(str(src_uuid), "")))
        if not src_fam:
            continue
        for target in targets or ():
            pair = (str(src_uuid), str(target))
            if pair in seen:
                continue
            dst_path = path_by_uuid.get(pair[1])
            if not dst_path:
                continue
            dst_fam = import_family(language_for_path(dst_path))
            if dst_fam and dst_fam != src_fam:
                seen.add(pair)
                out.append(pair)
    return out


def purge_incompatible_import_edges(collection: Any,
                                    stored_refs: Mapping[str, Sequence[Any]],
                                    module_cache: Mapping[Any, Any],
                                    *, enabled: Optional[bool] = None) -> int:
    """Delete every provably wrong stored ``imports`` edge, one beacon at a time.

    ``stored_refs`` is the analyzer's already-loaded ``{module_uuid:
    [target_uuid, ...]}`` scan and ``module_cache`` its ``{(project_source,
    path) | path: uuid}`` map, so a walk with nothing to delete costs no
    Weaviate call. Each delete is ``data.reference_delete`` with a beacon
    qualified by ``collection.name`` (the analyzer writes class-qualified
    beacons; see ``codegraph_ref_dedup``), so a module's other edges are never
    touched and its ref set is never replaced. A failed delete is logged and
    skipped; the next walk retries it. Returns the number of edges deleted.

    ``enabled`` defaults to :data:`DELETE_INCOMPATIBLE_IMPORT_EDGES`.
    """
    if enabled is None:
        enabled = DELETE_INCOMPATIBLE_IMPORT_EDGES
    if not enabled or not stored_refs or collection is None:
        return 0
    path_by_uuid: Dict[str, str] = {}
    for key, uuid in module_cache.items():
        path = _path_of_key(key)
        if path and uuid:
            path_by_uuid[str(uuid)] = path
    edges = incompatible_import_edges(stored_refs, path_by_uuid)
    if not edges:
        return 0
    to_ref: Callable[[str], Any] = _qualified_reference_builder(getattr(collection, "name", None))
    deleted = 0
    for src_uuid, dst_uuid in edges:
        try:
            collection.data.reference_delete(
                from_uuid=src_uuid, from_property="imports", to=to_ref(dst_uuid))
            deleted += 1
        except Exception as exc:  # noqa: BLE001 — one failed delete never aborts the walk
            logger.warning("code graph: could not delete the wrong-language imports edge "
                           "%s -> %s (%s); the next walk retries it", src_uuid, dst_uuid, exc)
    return deleted


def _qualified_reference_builder(collection_name: Optional[str]) -> Callable[[str], Any]:
    """``uuid`` → the ``to=`` argument naming ``collection_name`` (CodeModule
    edges point into their own collection). A bare UUID when the client's
    ``ReferenceToMulti`` is unavailable or the collection is unnamed."""
    if collection_name:
        try:
            from weaviate.classes.data import ReferenceToMulti  # noqa: PLC0415
        except ImportError:
            ReferenceToMulti = None  # type: ignore[assignment]  # noqa: N806
        if ReferenceToMulti is not None:
            return lambda uuid: ReferenceToMulti(target_collection=collection_name, uuids=uuid)
    return lambda uuid: uuid
