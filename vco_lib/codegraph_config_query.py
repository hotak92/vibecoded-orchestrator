# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Config VALUES ↔ the code that reads them: the ``config_users`` /
``config_of`` structure queries (v0.2.105).

The code graph indexes ``.tweakers`` / ``.ini`` / ``.json`` files
(:mod:`vco_lib.codegraph_lang.config`): one ``CodeClass`` row per SECTION,
whose ``class_body`` holds one ``key = value [(default)]  // description``
line per key, and one ``CodeInteraction`` LINK row (``interaction_type ==
"config"``) per (code entity, config target) — :mod:`vco_lib.codegraph_config_refs`.
This module is the ONE implementation of the two questions those rows answer,
called by BOTH the MCP (``weaviate_mcp/server.py::query_code_structure``) and
the CLI (``templates/scripts/query_code_graph.py structure``) — shared code,
never a mirror:

* :func:`config_users` — a key (``AimPID_P``), ``section/key``, a section, a
  config file path, or a logical endpoint (``ini:GENERAL/ggv_file``) → every
  code site that reads it, each with the VALUE it reads.
* :func:`config_of` — a function ``full_name`` or a module path → every config
  value that code reads.

Rules the two share:

* **Values come from the section rows**, read back with
  :func:`~vco_lib.codegraph_lang.config.parse_body_line`; nothing is copied
  onto the link rows. A JSON array value is stored as its preview plus its
  length, so it is shown that way.
* **One root.** Every code row carries ``project_source`` (the root it was
  walked from — the project or one extra code folder). A site is paired only
  with definitions from its OWN root (exact string equality; a property
  filter would be word-tokenized), so a project's code never shows an extra
  folder's values or vice versa. A ``config_of`` target resolves to one entity,
  hence one root.
* **Narrow, then confirm.** Every Weaviate filter here is a word-tokenized
  NARROWING read; each row is confirmed in Python
  (:func:`vco_lib.weaviate_exact_match.fetch_matching_rows`).
* **Chunked sections.** An over-budget section is stored as several chunk
  rows sharing ``full_name``; their lines are parsed together and only keys
  listed in ``methods`` are kept (a chunk header or a split line is skipped;
  of two overlapping copies of a key, the longer value wins).

``filter_cls`` (``weaviate.classes.query.Filter``) and ``ref_cls``
(``QueryReference``) are injected so tests run on in-memory fakes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Set, Tuple

import vco_lib.weaviate_exact_match as _wem
from vco_lib.codegraph_config_refs import (
    INTERACTION_TYPE,
    file_endpoint,
    key_endpoint,
    key_lookup_names,
    section_endpoint,
)
from vco_lib.codegraph_lang.config import CONFIG_FORMATS, config_format, parse_body_line
from vco_lib.codegraph_lookup import code_identity_rows, pick_canonical_chunk
from vco_lib.codegraph_references import read_cross_reference

__all__ = [
    "CONFIG_LANGUAGES",
    "CONFIG_QUERY_TYPES",
    "DEFAULT_LIMIT",
    "config_of",
    "config_users",
    "format_config_result",
    "is_config_link",
    "is_config_row",
    "run_config_query",
]

#: The ``language`` values config rows carry (one per format).
CONFIG_LANGUAGES = frozenset(CONFIG_FORMATS.values())
CONFIG_QUERY_TYPES = ("config_users", "config_of")
#: Link rows returned per query (the ``truncated`` flag says when it is hit).
DEFAULT_LIMIT = 50
#: Keys of a SECTION shown beside a site that names the whole section.
SECTION_VALUES_SHOWN = 10
#: Above this many endpoints the link read narrows on the format only.
_MAX_ENDPOINT_CLAUSES = 60
#: Section rows (chunks included) read for one target.
_MAX_SECTION_ROWS = 2000

_SECTION_PROPS = [
    "name", "full_name", "file_path", "language", "class_body", "methods",
    "start_line", "chunk_num", "project", "project_source",
]
_LINK_PROPS = [
    "interaction_type", "protocol", "endpoint", "raw_target", "confidence",
    "file_path", "project", "project_source",
]


# ---------------------------------------------------------------------------
# Row predicates (also the search-scope rules in weaviate_mcp/code_ranking.py)
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def is_config_row(props: Mapping[str, Any]) -> bool:
    """A config file (CodeModule) or section (CodeClass) row."""
    return _text(props.get("language")).lower() in CONFIG_LANGUAGES


def is_config_link(props: Mapping[str, Any]) -> bool:
    """A code → config LINK row (CodeInteraction)."""
    return _text(props.get("interaction_type")).lower() == INTERACTION_TYPE


def _root(props: Mapping[str, Any]) -> str:
    return _text(props.get("project_source"))


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Section rows → definitions
# ---------------------------------------------------------------------------

@dataclass
class _Section:
    fmt: str
    name: str
    file_path: str
    root: str
    line: Any
    #: key path -> (value, is_default, description), in file order.
    keys: Dict[str, Tuple[str, bool, str]] = field(default_factory=dict)

    def endpoints(self) -> Set[str]:
        eps = {section_endpoint(self.fmt, self.name)}
        eps |= {key_endpoint(self.fmt, self.name, k) for k in self.keys}
        return eps


def _sections_from_rows(rows: Iterable[Any]) -> List[_Section]:
    """Group a read's rows (chunks included) into parsed sections."""
    groups: Dict[Tuple[str, str, str], List[Mapping[str, Any]]] = {}
    for obj in rows:
        props = getattr(obj, "properties", None) or {}
        if not is_config_row(props):
            continue
        key = (_root(props), _text(props.get("file_path")),
               _text(props.get("full_name")) or _text(props.get("name")))
        groups.setdefault(key, []).append(props)
    sections: List[_Section] = []
    for (root, path, _full), chunks in sorted(groups.items()):
        chunks.sort(key=lambda p: p.get("chunk_num") or 0)
        head = chunks[0]
        allowed = {m for c in chunks for m in (c.get("methods") or []) if isinstance(m, str)}
        sec = _Section(
            fmt=_text(head.get("language")).lower(), name=_text(head.get("name")),
            file_path=path, root=root, line=head.get("start_line"),
        )
        for chunk in chunks:
            for line in str(chunk.get("class_body") or "").splitlines():
                parsed = parse_body_line(line.strip())
                if parsed is None:
                    continue
                k, value, is_default, desc = parsed
                if allowed and k not in allowed:
                    continue
                prev = sec.keys.get(k)
                if prev is None or len(value) > len(prev[0]):
                    sec.keys[k] = (value, is_default, desc)
        sections.append(sec)
    return sections


def _key_value(sec: _Section, key: str) -> Dict[str, Any]:
    value, is_default, desc = sec.keys[key]
    out: Dict[str, Any] = {
        "kind": "key", "format": sec.fmt, "file_path": sec.file_path,
        "section": sec.name, "line": sec.line, "key": key, "value": value,
    }
    if is_default:
        out["default"] = True
    if desc:
        out["description"] = desc
    return out


def _section_value(sec: _Section) -> Dict[str, Any]:
    keys = list(sec.keys)
    out: Dict[str, Any] = {
        "kind": "section", "format": sec.fmt, "file_path": sec.file_path,
        "section": sec.name, "line": sec.line,
        "keys": [_key_value(sec, k) for k in keys[:SECTION_VALUES_SHOWN]],
    }
    if len(keys) > SECTION_VALUES_SHOWN:
        out["more_keys"] = len(keys) - SECTION_VALUES_SHOWN
    return out


def _file_value(fmt: str, path: str, sections: List[_Section]) -> Dict[str, Any]:
    return {"kind": "file", "format": fmt, "file_path": path,
            "sections": [s.name for s in sections]}


@dataclass
class _Target:
    """A parsed ``config_users`` target / link endpoint."""

    raw: str
    fmt: str = ""            # set by the endpoint form ``<fmt>:...``
    rest: str = ""           # the endpoint body after ``<fmt>:``

    @classmethod
    def parse(cls, raw: str) -> "_Target":
        raw = raw.strip()
        head, sep, rest = raw.partition(":")
        if sep and head.lower() in CONFIG_LANGUAGES and rest:
            return cls(raw=raw, fmt=head.lower(), rest=rest)
        return cls(raw=raw)

    def section_candidates(self) -> List[str]:
        """Every section name the target can name (``a/b/c`` → a, a/b, a/b/c)."""
        text = self.rest if self.fmt else self.raw
        if self.fmt and text.startswith("file:"):
            return []
        out = [text]
        idx = text.find("/")
        while idx > 0:
            out.append(text[:idx])
            idx = text.find("/", idx + 1)
        return out

    def file_name(self) -> str:
        if self.fmt:
            return self.rest[5:] if self.rest.startswith("file:") else ""
        return self.raw if config_format(self.raw) else ""


def _match(target: _Target, sec: _Section) -> Tuple[str, List[str]]:
    """How ``target`` names ``sec``: ``("file"|"section"|"key", [key paths])``
    or ``("", [])``."""
    if target.fmt and target.fmt != sec.fmt:
        return "", []
    fname = target.file_name()
    if fname:
        if target.fmt or ("/" not in fname and "\\" not in fname):
            same = _basename(sec.file_path) == fname        # a bare file name
        else:
            same = _wem.names_same_path(sec.file_path, fname)  # a path, any spelling
        if same:
            return "file", []
        if target.fmt:
            return "", []
    text = target.rest if target.fmt else target.raw
    if text == sec.name:
        return "section", []
    if text.startswith(sec.name + "/") and text[len(sec.name) + 1:] in sec.keys:
        return "key", [text[len(sec.name) + 1:]]
    if target.fmt:
        return "", []
    keys = [k for k in sec.keys if text in key_lookup_names(sec.fmt, k)]
    return ("key", keys) if keys else ("", [])


def _any_of(clauses: List[Any]) -> Any:
    flt = clauses[0]
    for extra in clauses[1:]:
        flt = flt | extra
    return flt


def _project_clause(filter_cls: Any, flt: Any, project: str) -> Any:
    return flt & filter_cls.by_property("project").equal(project) if project else flt


def _read_sections(class_coll: Any, target: _Target, project: str, *, filter_cls: Any) -> List[_Section]:
    """The section rows ``target`` could name, confirmed and parsed."""
    narrowing = [filter_cls.by_property("name").equal(n) for n in target.section_candidates() if n]
    fname = target.file_name()
    if fname:
        narrowing.append(filter_cls.by_property("file_path").equal(fname))
    if not target.fmt:
        narrowing.append(filter_cls.by_property("methods").equal(target.raw))
    if not narrowing:
        return []
    langs = [target.fmt] if target.fmt else sorted(CONFIG_LANGUAGES)
    flt = _any_of(narrowing) & _any_of([filter_cls.by_property("language").equal(lang) for lang in langs])
    flt = _project_clause(filter_cls, flt, project)
    rows = _wem.fetch_matching_rows(
        class_coll, flt, is_config_row, max_matches=_MAX_SECTION_ROWS,
        return_properties=_SECTION_PROPS,
    )
    return [s for s in _sections_from_rows(rows) if _match(target, s)[0]]


class _Definitions:
    """Endpoint → definitions in one root, read once per query (cached)."""

    def __init__(self, class_coll: Any, project: str, filter_cls: Any) -> None:
        self._coll, self._project, self._filter = class_coll, project, filter_cls
        self._cache: Dict[str, List[_Section]] = {}

    def values(self, endpoint: str, root: str) -> List[Dict[str, Any]]:
        if endpoint not in self._cache:
            try:
                self._cache[endpoint] = _read_sections(
                    self._coll, _Target.parse(endpoint), self._project, filter_cls=self._filter)
            except Exception:  # noqa: BLE001 — a value read never fails the query
                self._cache[endpoint] = []
        target = _Target.parse(endpoint)
        out: List[Dict[str, Any]] = []
        files: Dict[Tuple[str, str], List[_Section]] = {}
        for sec in self._cache[endpoint]:
            if sec.root != root:
                continue
            kind, keys = _match(target, sec)
            if kind == "key":
                out.extend(_key_value(sec, k) for k in keys)
            elif kind == "section":
                out.append(_section_value(sec))
            elif kind == "file":
                files.setdefault((sec.fmt, sec.file_path), []).append(sec)
        out.extend(_file_value(fmt, path, secs) for (fmt, path), secs in sorted(files.items()))
        return out


# ---------------------------------------------------------------------------
# Link rows → sites
# ---------------------------------------------------------------------------

def _link_references(ref_cls: Any) -> Dict[str, Any]:
    if ref_cls is None:
        return {}
    return {"return_references": [
        ref_cls(link_on="source_function", return_properties=["full_name", "file_path", "start_line"]),
        ref_cls(link_on="source_module", return_properties=["path"]),
    ]}


def _fetch_links(coll: Any, flt: Any, accept: Callable[[Mapping[str, Any]], bool],
                 limit: int, ref_cls: Any) -> list:
    kwargs: Dict[str, Any] = {"return_properties": _LINK_PROPS, **_link_references(ref_cls)}
    try:
        return _wem.fetch_matching_rows(coll, flt, accept, max_matches=limit + 1, **kwargs)
    except Exception:  # noqa: BLE001 — a schema without the refs: read the rows bare
        if not ref_cls:
            raise
        return _wem.fetch_matching_rows(coll, flt, accept, max_matches=limit + 1,
                                        return_properties=_LINK_PROPS)


def _site(obj: Any) -> Dict[str, Any]:
    props = getattr(obj, "properties", None) or {}
    site: Dict[str, Any] = {"file_path": _text(props.get("file_path"))}
    fns = read_cross_reference(obj, "source_function")
    if fns:
        fp = getattr(fns[0], "properties", None) or {}
        site["full_name"] = _text(fp.get("full_name"))
        if fp.get("start_line") is not None:
            site["line"] = fp.get("start_line")
        site["file_path"] = _text(fp.get("file_path")) or site["file_path"]
    else:
        mods = read_cross_reference(obj, "source_module")
        if mods:
            site["file_path"] = _text((getattr(mods[0], "properties", None) or {}).get("path")) \
                or site["file_path"]
        site["full_name"] = site["file_path"]
        site["top_level"] = True
    return site


def _link_entry(obj: Any, defs: _Definitions) -> Dict[str, Any]:
    props = getattr(obj, "properties", None) or {}
    endpoint = _text(props.get("endpoint"))
    entry = {
        **_site(obj),
        "endpoint": endpoint,
        "format": _text(props.get("protocol")),
        "raw_target": _text(props.get("raw_target")),
        "confidence": _text(props.get("confidence")),
        "values": defs.values(endpoint, _root(props)),
    }
    if _root(props):
        entry["project_source"] = _root(props)
    return entry


def _result(query_type: str, target: str, entries: List[Dict[str, Any]], limit: int,
            **extra: Any) -> Dict[str, Any]:
    truncated = len(entries) > limit
    entries = entries[:limit]
    return {"success": True, "query_type": query_type, "target": target,
            "count": len(entries), "results": entries,
            "truncated": truncated, "limit": limit, **extra}


# ---------------------------------------------------------------------------
# The two queries
# ---------------------------------------------------------------------------

def config_users(
    coll_for: Callable[[str], Any], target: str, project: str = "", *,
    filter_cls: Any, ref_cls: Any = None, limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """Every code site that reads the config ``target``, with the value."""
    class_coll = coll_for("CodeClass")
    parsed = _Target.parse(target)
    sections = _read_sections(class_coll, parsed, project, filter_cls=filter_cls)
    if not sections:
        return {"success": False, "query_type": "config_users", "target": target,
                "error": (f"No config key, section or file named '{target}' in the code graph. "
                          f"Targets: a key, 'section/key', a section, a config file path, or "
                          f"'<format>:<section>/<key>'; config files are .tweakers/.ini/.json "
                          f"(files under bin/ are not indexed).")}

    # The endpoints the target covers, per ROOT: a site is accepted only when
    # its own root defines what it reads.
    by_root: Dict[str, Set[str]] = {}
    definitions: List[Dict[str, Any]] = []
    files: Dict[Tuple[str, str, str], List[_Section]] = {}
    for sec in sections:
        kind, keys = _match(parsed, sec)
        eps = by_root.setdefault(sec.root, set())
        if kind == "key":
            eps |= {key_endpoint(sec.fmt, sec.name, k) for k in keys}
            definitions.extend(_key_value(sec, k) for k in keys)
        elif kind == "section":
            eps |= sec.endpoints()
            definitions.append(_section_value(sec))
        else:
            eps |= sec.endpoints() | {file_endpoint(sec.fmt, _basename(sec.file_path))}
            files.setdefault((sec.root, sec.fmt, sec.file_path), []).append(sec)
    definitions.extend(_file_value(fmt, path, secs) for (_r, fmt, path), secs in sorted(files.items()))

    all_eps = sorted(set().union(*by_root.values()))
    if len(all_eps) <= _MAX_ENDPOINT_CLAUSES:
        narrow = _any_of([filter_cls.by_property("endpoint").equal(e) for e in all_eps])
    else:
        fmts = sorted({s.fmt for s in sections})
        narrow = _any_of([filter_cls.by_property("protocol").equal(f) for f in fmts])
    flt = _project_clause(
        filter_cls, filter_cls.by_property("interaction_type").equal(INTERACTION_TYPE) & narrow, project)

    def accept(props: Mapping[str, Any]) -> bool:
        return is_config_link(props) and _text(props.get("endpoint")) in by_root.get(_root(props), ())

    links = _fetch_links(coll_for("CodeInteraction"), flt, accept, limit, ref_cls)
    defs = _Definitions(class_coll, project, filter_cls)
    entries = [_link_entry(obj, defs) for obj in links]
    entries.sort(key=lambda e: (e.get("project_source", ""), e.get("file_path", ""),
                                e.get("full_name", ""), e["endpoint"]))
    return _result("config_users", target, entries, limit, definitions=definitions)


def config_of(
    coll_for: Callable[[str], Any], target: str, project: str = "", *,
    filter_cls: Any, ref_cls: Any = None, limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """Every config value the function / module ``target`` reads."""
    func = pick_canonical_chunk(code_identity_rows(
        coll_for("CodeFunction"), "full_name", target, project,
        filter_cls=filter_cls, tolerant_limit=8,
    ))
    if func is not None:
        ref = filter_cls.by_ref("source_function").by_id().equal(str(func.uuid))
    else:
        mods = code_identity_rows(
            coll_for("CodeModule"), "path", target, project,
            filter_cls=filter_cls, tolerant_limit=1,
        )
        if not mods:
            return {"success": False, "query_type": "config_of", "target": target,
                    "error": f"Function or module '{target}' not found in the code graph."}
        ref = filter_cls.by_ref("source_module").by_id().equal(str(mods[0].uuid))
    flt = ref & filter_cls.by_property("interaction_type").equal(INTERACTION_TYPE)
    links = _fetch_links(coll_for("CodeInteraction"), flt, is_config_link, limit, ref_cls)
    defs = _Definitions(coll_for("CodeClass"), project, filter_cls)
    entries = [_link_entry(obj, defs) for obj in links]
    entries.sort(key=lambda e: (e.get("line") or 0, e["endpoint"]))
    return _result("config_of", target, entries, limit)


def run_config_query(
    query_type: str, coll_for: Callable[[str], Any], target: str, project: str = "", *,
    filter_cls: Any, ref_cls: Any = None, limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    fn = config_users if query_type == "config_users" else config_of
    return fn(coll_for, target, project, filter_cls=filter_cls, ref_cls=ref_cls, limit=limit)


# ---------------------------------------------------------------------------
# Text rendering (the CLI's output; the MCP returns the dict as JSON)
# ---------------------------------------------------------------------------

def _value_line(v: Mapping[str, Any]) -> List[str]:
    where = f"{v.get('file_path', '')} [{v.get('section', '')}]"
    if v.get("kind") == "key":
        line = f"{v['key']} = {v['value']}"
        if v.get("default"):
            line += " (default)"
        if v.get("description"):
            line += f"  // {v['description']}"
        return [f"{line}    ({where})"]
    if v.get("kind") == "section":
        lines = [f"section {where}"]
        lines += ["  " + _value_line(k)[0].split("    (")[0] for k in v.get("keys", [])]
        if v.get("more_keys"):
            lines.append(f"  … (+{v['more_keys']} more keys)")
        return lines
    return [f"file {v.get('file_path', '')}: sections {', '.join(v.get('sections', []))}"]


def format_config_result(result: Mapping[str, Any]) -> str:
    """Human-readable text for a :func:`config_users` / :func:`config_of` result."""
    if not result.get("success"):
        return f"❌ {result.get('error', 'config query failed')}"
    qt, target = result["query_type"], result["target"]
    out: List[str] = []
    if qt == "config_users":
        out.append(f"\n⚙️  Code that reads config '{target}':")
    else:
        out.append(f"\n⚙️  Config values read by '{target}':")
    out.append(f"   Found {result['count']} link(s):\n")
    for e in result["results"]:
        site = e.get("full_name") or e.get("file_path", "")
        loc = f"{e.get('file_path', '')}:{e['line']}" if e.get("line") else e.get("file_path", "")
        out.append(f"   [{e.get('confidence', '?')}] {site}  ({loc})")
        out.append(f"     reads {e['endpoint']}  (as \"{e.get('raw_target', '')}\")")
        if not e.get("values"):
            out.append("     value: (not defined in this folder's config files)")
        for v in e.get("values", []):
            for i, line in enumerate(_value_line(v)):
                out.append(("     value: " if i == 0 else "            ") + line)
        out.append("")
    if result.get("truncated"):
        out.append(f"⚠️  Result list capped at {result['limit']}.")
    return "\n".join(out)
