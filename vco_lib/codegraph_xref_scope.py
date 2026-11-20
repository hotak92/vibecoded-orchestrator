# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The cross-reference pass of a SCOPED analyzer run reads only what its files need.

v0.2.107 (CG-3, A-011). Every analyzer run — a 2-file ``--only-files-from``
batch included — used to load the whole graph to build cross-references: all
modules with their ``imports`` resolved, all classes, all functions with their
``calls`` resolved. Measured on a large project: 174 catch-up batches in five
days, each indexing nothing, 189 s each. And when that load failed (the first
page over the gRPC limit) the walk went on with an empty map of stored
references and re-added every edge it found.

A scoped run (``changed_files`` given) now reads, through the ONE narrowed
read (:func:`vco_lib.codegraph_resync.fetch_narrowed_rows`, filter + exact
compare in Python):

* the function / class / module rows of the changed files (their stored
  ``call_names``, ``n_callers``, ``calls`` / ``imports`` beacons);
* per distinct callee / base-class name those files use, the rows that name
  could resolve to (``full_name`` equal, or the leaf ``name`` equal) — ids,
  names, source and ``n_callers`` only;
* per resolved import target, the module row at that path.

No function or class row is listed whole. Import names are resolved against
the repository's tracked files (``git ls-files``); only without git is the
module class listed, ``path`` alone, and only when a changed file imports.

``n_callers`` on a scoped run is a DELTA: a target's stored count minus the
edges the changed functions had before (their stored ``call_names``) plus the
edges they have now. A target whose count was never computed (``None``) is
left for the next whole-repo walk, which recounts everything from scratch; so
is every count when any lookup failed (:attr:`ScopedXref.failed`).

The whole-class load stays for whole-repo walks (the analyzer's
``_populate_caches_from_weaviate``). Either load failing skips the whole
cross-reference pass — nothing linked, nothing re-added.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

logger = logging.getLogger("vco_lib.codegraph_xref_scope")

_FUNC_PROPS = ["full_name", "chunk_num", "file_path", "call_names", "n_callers", "project_source"]
_CLASS_PROPS = ["full_name", "chunk_num", "file_path", "project_source"]
_MODULE_PROPS = ["path", "project_source", "import_names"]
_CANDIDATE_PROPS = ["full_name", "name", "chunk_num", "n_callers", "project_source"]


def _eq(prop: str, value: str) -> Any:
    from weaviate.classes.query import Filter

    return Filter.by_property(prop).equal(value)


def _ref(link_on: str, props: List[str]) -> Any:
    from weaviate.classes.query import QueryReference

    return QueryReference(link_on=link_on, return_properties=props)


def tracked_module_paths(repo_root: Optional[Path]) -> Optional[List[str]]:
    """Repo-relative paths git tracks under ``repo_root``; None when git
    cannot answer (not a repository, git missing, timeout)."""
    if repo_root is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split("\0") if p]


class ScopedXref:
    """The scoped loader + lazy lookups for one analyzer's cross-reference pass.

    ``analyzer`` is the ``CodeGraphAnalyzer``: this fills the same caches its
    whole-class loader fills (``function_cache``, ``class_cache``,
    ``module_cache``, ``module_imports`` and the ``_xref_*`` side maps).
    """

    def __init__(self, analyzer: Any, changed_files: Iterable[str],
                 sources: Iterable[str], repo_root: Optional[Path]):
        self.a = analyzer
        self.changed: Set[str] = {str(p) for p in changed_files}
        self.sources: Set[str] = {s for s in sources if s}
        self.repo_root = repo_root
        self.failed = False
        self.reads = 0
        self._looked_funcs: Set[str] = set()
        self._looked_classes: Set[str] = set()
        self._looked_modules: Set[str] = set()
        #: changed function uuid -> its STORED call_names (before this pass).
        self.old_calls: Dict[str, List[str]] = {}

    # ── reads ─────────────────────────────────────────────────────────────

    def _read(self, coll: Any, props: List[str], flt: Any, refs: Any = None) -> Optional[list]:
        from vco_lib import codegraph_guards as _guards
        from vco_lib.codegraph_resync import fetch_narrowed_rows

        if coll is None:
            return []
        self.reads += 1
        _guards.walk_count("xref_reads")
        rows = fetch_narrowed_rows(coll, props, flt, return_references=refs)
        if rows is None:
            self.failed = True
        return rows

    def _owned(self, props: Mapping[str, Any]) -> bool:
        src = str(props.get("project_source") or "").strip()
        return not src or not self.sources or src in self.sources

    def load(self) -> bool:
        """Read the rows of the changed files into the analyzer's caches.
        False = a read failed; the caller skips the whole pass."""
        from vco_lib.codegraph_references import reference_target_uuids

        a = self.a
        a._xref_stored_import_refs = {}
        a._xref_class_file_path = {}
        a._xref_file_path = {}
        a._xref_stored_calls = {}
        a._xref_stored_ncallers = {}
        a._xref_source = {}
        a._xref_stored_call_refs = {}
        for rel in sorted(self.changed):
            funcs = self._read(a.functions_collection, _FUNC_PROPS, _eq("file_path", rel),
                               _ref("calls", ["full_name"]))
            classes = self._read(a.classes_collection, _CLASS_PROPS, _eq("file_path", rel))
            mods = self._read(a.modules_collection, _MODULE_PROPS, _eq("path", rel),
                              _ref("imports", ["path"]))
            if funcs is None or classes is None or mods is None:
                return False
            for obj in funcs:
                p = obj.properties or {}
                name = p.get("full_name") or ""
                if p.get("file_path") != rel or not name or not self._owned(p):
                    continue
                if a._prefer_canonical_chunk(a.function_cache, name, obj):
                    u = str(obj.uuid)
                    a.function_cache[name] = u
                    a._xref_file_path[u] = rel
                    a._xref_stored_calls[u] = p.get("call_names")
                    a._xref_stored_ncallers[u] = p.get("n_callers")
                    a._xref_stored_call_refs[u] = reference_target_uuids(obj, "calls")
                    a._xref_source[u] = str(p.get("project_source") or "").strip()
                    stored = p.get("call_names")
                    self.old_calls[u] = list(stored) if isinstance(stored, list) else []
            for obj in classes:
                p = obj.properties or {}
                name = p.get("full_name") or ""
                if p.get("file_path") != rel or not name or not self._owned(p):
                    continue
                a._xref_class_file_path[str(obj.uuid)] = rel
                if a._prefer_canonical_chunk(a.class_cache, name, obj):
                    a.class_cache[name] = str(obj.uuid)
            for obj in mods:
                p = obj.properties or {}
                if p.get("path") != rel or not self._owned(p):
                    continue
                src = str(p.get("project_source") or "")
                a.module_cache[(src, rel)] = str(obj.uuid)
                a._xref_stored_import_refs[str(obj.uuid)] = reference_target_uuids(obj, "imports")
                # The stored edges' targets: the wrong-language edge purge
                # judges an edge only when both ends are known rows of THIS
                # class, so each target is point-read (path only) here.
                for tgt in a._xref_stored_import_refs[str(obj.uuid)]:
                    self._known_module(tgt)
                if p.get("import_names") and rel not in a.module_imports:
                    a.module_imports[rel] = p.get("import_names")
        self._looked_funcs |= set(a.function_cache)
        self._looked_classes |= set(a.class_cache)
        return True

    def _known_module(self, uuid: str) -> None:
        """Add the module row ``uuid`` (if it is one of this class) to the cache."""
        from vco_lib import codegraph_guards as _guards
        from vco_lib.codegraph_bounded_read import fetch_object_by_id

        a = self.a
        if uuid in set(a.module_cache.values()):
            return
        self.reads += 1
        _guards.walk_count("xref_reads")
        try:
            obj = fetch_object_by_id(a.modules_collection, uuid, "codegraph_xref_scope.import_target",
                                     return_properties=["path", "project_source"])
        except Exception:  # noqa: BLE001 — unknown target: the purge keeps the edge
            return
        p = (getattr(obj, "properties", None) or {}) if obj is not None else {}
        if p.get("path"):
            a.module_cache.setdefault((str(p.get("project_source") or ""), str(p["path"])), str(uuid))

    def _candidates(self, coll: Any, name: str, looked: Set[str]) -> List[Any]:
        """Rows ``name`` could resolve to: ``full_name == name``, plus — for a
        bare name — rows whose leaf ``name`` equals it. Exact compare."""
        if name in looked or coll is None:
            return []
        looked.add(name)
        out: List[Any] = []
        reads = [("full_name", name)]
        if "." not in name:
            reads.append(("name", name))
        for prop, value in reads:
            rows = self._read(coll, _CANDIDATE_PROPS, _eq(prop, value))
            for obj in rows or []:
                p = obj.properties or {}
                full = str(p.get("full_name") or "")
                leaf = full.rsplit(".", 1)[-1]
                if full == name or (prop == "name" and leaf == name):
                    out.append(obj)
        return out

    def ensure_call(self, call_name: str, short_index: Dict[str, List[str]]) -> None:
        """Make the functions ``call_name`` could resolve to known to the
        analyzer's resolver (cache, short-name index, sources, stored counts)."""
        a = self.a
        for obj in self._candidates(a.functions_collection, call_name, self._looked_funcs):
            p = obj.properties or {}
            full = str(p.get("full_name") or "")
            new = full not in a.function_cache
            if a._prefer_canonical_chunk(a.function_cache, full, obj):
                u = str(obj.uuid)
                a.function_cache[full] = u
                a._xref_source[u] = str(p.get("project_source") or "").strip()
                a._xref_stored_ncallers.setdefault(u, p.get("n_callers"))
                if new:
                    short_index.setdefault(full.rsplit(".", 1)[-1], []).append(full)

    def ensure_bases(self, signature: str, short_index: Dict[str, List[str]]) -> None:
        """Same for the base classes a ``class Foo(Bar, Baz)`` line names."""
        a = self.a
        match = re.search(r"\(([^)]+)\)", signature or "")
        for base in (b.strip() for b in (match.group(1).split(",") if match else [])):
            if not base:
                continue
            for obj in self._candidates(a.classes_collection, base, self._looked_classes):
                full = str((obj.properties or {}).get("full_name") or "")
                new = full not in a.class_cache
                if a._prefer_canonical_chunk(a.class_cache, full, obj):
                    a.class_cache[full] = str(obj.uuid)
                    if new:
                        short_index.setdefault(full.rsplit(".", 1)[-1], []).append(full)

    def module_paths(self) -> List[str]:
        """The paths import names resolve against: the repository's tracked
        files; without git, the stored module paths (one cursor listing of
        ``path`` only — never a body or a reference). Nothing is read when
        the changed files carry no import name."""
        if not self.a.module_imports:
            return []
        paths = tracked_module_paths(self.repo_root)
        if paths is not None:
            return paths
        from vco_lib import codegraph_guards as _guards
        from vco_lib.codegraph_cursor import CursorUndeterminable, iter_class

        _guards.walk_count("xref_reads")
        try:
            return [str((o.properties or {}).get("path") or "") for o in iter_class(
                self.a.modules_collection, "codegraph_xref_scope.module_paths",
                return_properties=["path"])]
        except CursorUndeterminable:
            self.failed = True
            return []

    def ensure_module(self, path: str, by_path: Dict[str, str]) -> Optional[str]:
        """The module row at ``path`` (the walk's own source preferred)."""
        if path in by_path:
            return by_path[path]
        if path in self._looked_modules:
            return None
        self._looked_modules.add(path)
        best: Optional[str] = None
        for obj in self._read(self.a.modules_collection, ["path", "project_source"],
                              _eq("path", path)) or []:
            p = obj.properties or {}
            if p.get("path") != path or not self._owned(p):
                continue
            best = str(obj.uuid)
            if str(p.get("project_source") or "") in self.sources:
                break
        if best:
            by_path[path] = best
        return best

    # ── n_callers on a scoped run ─────────────────────────────────────────

    def caller_counts(self, inbound: Mapping[str, int], short_index: Dict[str, List[str]]) -> Dict[str, int]:
        """``{target uuid: new n_callers}`` for the targets whose count this
        run can know exactly: stored count − old edges + new edges. Empty when
        a lookup failed (the next whole-repo walk recounts)."""
        a = self.a
        old: Dict[str, int] = {}
        name_by_uuid = {u: n for n, u in a.function_cache.items()}
        for caller, names in self.old_calls.items():
            for cn in names:
                self.ensure_call(cn, short_index)
                tgt = a._resolve_call_target(cn, name_by_uuid.get(caller, ""), short_index,
                                             a._row_source(caller))
                if tgt is not None:
                    old[tgt] = old.get(tgt, 0) + 1
        if self.failed:
            logger.warning("scoped cross-references: a lookup failed; n_callers left for the next full walk")
            return {}
        out: Dict[str, int] = {}
        for tgt in set(old) | set(inbound):
            raw = a._xref_stored_ncallers.get(tgt)
            try:
                stored = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                stored = None
            if stored is None:
                continue
            out[tgt] = max(0, stored - old.get(tgt, 0) + inbound.get(tgt, 0))
        return out
