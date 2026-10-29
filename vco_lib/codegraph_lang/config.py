# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Config files in the code graph: ``.tweakers``, ``.ini`` and ``.json`` (v0.2.105).

These files hold the VALUES a program reads at run time, so the code graph
indexes them beside the code and links each key to the code that names it
(the links are :mod:`vco_lib.codegraph_config_refs`; this module is the
parsers and the extractor).

Row model (no new collection, no new property):

* one ``CodeModule`` per file — ``language`` is the FORMAT (``tweakers`` /
  ``ini`` / ``json``), ``module_summary`` lists the sections and keys;
* one ``CodeClass`` per SECTION (a ``.tweakers`` ``======`` group, an INI
  ``[section]``, a JSON top-level key whose value is an object; a synthetic
  ``(root)`` section holds top-level scalars and section-less INI keys).
  ``methods`` lists the section's key paths and ``class_body`` holds one line
  per key — ``key = value  // description`` — so every key's VALUE (and its
  default / description where the format has them) is searchable text and is
  part of the section's embedding.

Only these three extensions, nothing else (owner ruling 2026-10-15). A file
that does not parse is not indexed (one log line); its earlier rows are pruned
like a minified file's (S16-3). ``bin/`` stays ignored for every file type, as
for code (owner ruling Q1).

Pure: no analyzer state, no Weaviate, no network. Limits have env overrides
and there is no timeout anywhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from vco_lib.codegraph_entities import (
    CodeEntity,
    FileExtraction,
    KIND_CLASS,
    ModuleDescriptor,
)

#: extension -> format. The ONE home of "which files are config files".
CONFIG_FORMATS: Dict[str, str] = {".tweakers": "tweakers", ".ini": "ini", ".json": "json"}
#: The analyzer's dispatch key for all three formats (rows carry the format).
CONFIG_DISPATCH_KEY = "config"
#: Glob patterns for the walk (the analyzer's ``_FINDER_SPECS`` row reads this).
CONFIG_GLOBS: Tuple[str, ...] = tuple(f"*{ext}" for ext in CONFIG_FORMATS)
#: File names that carry a config extension but are not configuration: npm
#: lockfiles, minified JSON and VCO's own bundle manifest. Shared by the walk
#: and the single-file dispatch so the two entry points agree.
CONFIG_NAME_SKIP_SUFFIXES: Tuple[str, ...] = (
    "package-lock.json", "npm-shrinkwrap.json", ".lock.json", ".min.json",
    ".vco-manifest.json",
)

#: The synthetic section that holds keys outside any section.
ROOT_SECTION = "(root)"

MAX_BYTES_ENV = "VCO_CODEGRAPH_CONFIG_MAX_BYTES"
MAX_KEYS_ENV = "VCO_CODEGRAPH_CONFIG_MAX_KEYS"
DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_MAX_KEYS = 2000
#: JSON nesting depth below which an object is shown as a preview, not walked.
JSON_MAX_DEPTH = 6
#: Array previews: how many elements, and the longest rendered value.
PREVIEW_ITEMS = 8
MAX_VALUE_CHARS = 200


class ConfigParseError(ValueError):
    """The file is not valid in its format — it is not indexed."""


@dataclass
class ConfigKey:
    """One key. ``path`` is relative to its section (JSON: ``a.b``, ``a[].c``)."""

    path: str
    value: str
    line: int
    default: bool = False   # ``.tweakers`` ``default[v]``: the value IS the default
    description: str = ""

    @property
    def leaf(self) -> str:
        return key_leaf(self.path)


@dataclass
class ConfigSection:
    name: str
    signature: str
    start_line: int
    end_line: int
    doc: str = ""
    keys: List[ConfigKey] = field(default_factory=list)


@dataclass
class ConfigDoc:
    fmt: str
    header: str = ""
    sections: List[ConfigSection] = field(default_factory=list)
    total_keys: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def max_bytes() -> int:
    return _env_int(MAX_BYTES_ENV, DEFAULT_MAX_BYTES)


def max_keys() -> int:
    return _env_int(MAX_KEYS_ENV, DEFAULT_MAX_KEYS)


def config_format(path: "str | Path") -> str:
    """The format of a config file name, ``""`` for anything else (including
    the name-skipped lockfiles and manifests)."""
    name = Path(str(path)).name
    if any(name.endswith(s) for s in CONFIG_NAME_SKIP_SUFFIXES):
        return ""
    return CONFIG_FORMATS.get(Path(name).suffix.lower(), "")


def key_leaf(path: str) -> str:
    """The last component of a key path, without array markers."""
    return path.rsplit(".", 1)[-1].replace("[]", "")


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 1] + "…"


def render_json_value(value: Any) -> str:
    """A JSON value as one searchable line. Arrays of scalars become a
    truncated preview plus their length (owner ruling: never dropped)."""
    if isinstance(value, list):
        head = ", ".join(render_json_value(v) for v in value[:PREVIEW_ITEMS])
        more = ", …" if len(value) > PREVIEW_ITEMS else ""
        return _clip(f"[{head}{more}] (len={len(value)})")
    if isinstance(value, dict):
        return _clip("{" + ", ".join(list(value)[:PREVIEW_ITEMS]) + "}" + f" ({len(value)} keys)")
    if isinstance(value, str):
        return _clip(value)
    return _clip(json.dumps(value))


def body_line(key: ConfigKey) -> str:
    """One ``class_body`` line: ``key = value [(default)]  // description``."""
    line = f"{key.path} = {key.value}"
    if key.default:
        line += " (default)"
    if key.description:
        line += f"  // {key.description}"
    return line


_BODY_LINE_RE = re.compile(r"^(?P<key>.*?) = (?P<value>.*?)(?P<default> \(default\))?(?:  // (?P<desc>.*))?$")


def parse_body_line(line: str) -> Optional[Tuple[str, str, bool, str]]:
    """Inverse of :func:`body_line`: ``(key, value, is_default, description)``.

    The seam a query reads a key's value back from a stored section row with.
    ``None`` for a line that is not a key line.
    """
    m = _BODY_LINE_RE.match(line)
    if m is None:
        return None
    return m.group("key"), m.group("value"), bool(m.group("default")), m.group("desc") or ""


def _cap_keys(doc: ConfigDoc, limit: int) -> ConfigDoc:
    total = sum(len(s.keys) for s in doc.sections)
    doc.total_keys = total
    if total <= limit:
        return doc
    left = limit
    for section in doc.sections:
        section.keys = section.keys[:max(left, 0)]
        left -= len(section.keys)
    doc.truncated = True
    return doc


# ---------------------------------------------------------------------------
# .tweakers — ``======Group`` sections, ``>key:value  //description`` items
# ---------------------------------------------------------------------------

_TWEAK_BANNER_RE = re.compile(r"^prefix groups with", re.IGNORECASE)
_TWEAK_DEFAULT_RE = re.compile(r"^default\[(.*)\]$")


def parse_tweakers(text: str) -> ConfigDoc:
    doc = ConfigDoc(fmt="tweakers")
    root = ConfigSection(ROOT_SECTION, ROOT_SECTION, 1, 1)
    current = root
    sections: List[ConfigSection] = []
    seen_grammar = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("======"):
            seen_grammar = True
            name = line[6:].strip()
            if current is not root:
                current.end_line = lineno - 1
            current = ConfigSection(name, f"======{name}", lineno, lineno, doc=doc.header)
            sections.append(current)
            continue
        if line.startswith(">"):
            item = line[1:]
            if ":" not in item:
                continue  # an item with no value is not a key line
            seen_grammar = True
            key, rest = item.split(":", 1)
            value, _, desc = rest.partition("//")
            value = value.strip()
            m = _TWEAK_DEFAULT_RE.match(value)
            current.keys.append(ConfigKey(
                path=key.strip(), value=_clip(m.group(1) if m else value), line=lineno,
                default=m is not None, description=_clip(desc.strip()),
            ))
            current.end_line = lineno
            continue
        if not doc.header and not seen_grammar and not _TWEAK_BANNER_RE.match(line):
            doc.header = line  # line 1: the file kind ("AI Tweaks")
    if not seen_grammar:
        raise ConfigParseError("no ====== group and no >key:value item")
    if current is not root:
        current.end_line = max(current.end_line, current.start_line)
    if root.keys:
        root.end_line = root.keys[-1].line
        sections.insert(0, root)
    doc.sections = sections
    return doc


# ---------------------------------------------------------------------------
# .ini — custom line parser (configparser loses line numbers)
# ---------------------------------------------------------------------------

_INI_SECTION_RE = re.compile(r"^\[(?P<name>.+)\]\s*(?:[;#].*)?$")
_INI_KEY_RE = re.compile(r"^(?P<key>[^=:]+?)\s*[=:]\s*(?P<value>.*)$")


def parse_ini(text: str) -> ConfigDoc:
    doc = ConfigDoc(fmt="ini")
    root = ConfigSection(ROOT_SECTION, ROOT_SECTION, 1, 1)
    current = root
    sections: List[ConfigSection] = []
    comments: List[str] = []
    last_key: Optional[ConfigKey] = None
    continuation: List[str] = []

    def _flush() -> None:
        nonlocal continuation
        if last_key is not None and continuation:
            joined = " ".join([last_key.value] + continuation).strip()
            last_key.value = _clip(joined)
        continuation = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped[0] in ";#":
            comments.append(stripped.lstrip(";#").strip())
            continue
        if raw[0] in " \t" and last_key is not None:
            continuation.append(stripped)  # an indented line continues the value
            current.end_line = lineno
            continue
        _flush()
        m = _INI_SECTION_RE.match(stripped)
        if m is not None:
            current = ConfigSection(m.group("name").strip(), f"[{m.group('name').strip()}]",
                                    lineno, lineno, doc=_clip(" ".join(comments)))
            sections.append(current)
            comments, last_key = [], None
            continue
        km = _INI_KEY_RE.match(stripped)
        if km is not None:
            last_key = ConfigKey(path=km.group("key").strip(), value=_clip(km.group("value")),
                                 line=lineno)
        elif current is not root:
            last_key = ConfigKey(path=stripped, value="", line=lineno)  # a bare key
        else:
            raise ConfigParseError(f"line {lineno} is neither a section nor a key")
        current.keys.append(last_key)
        current.end_line = lineno
        comments = []
    _flush()
    if not sections and not root.keys:
        raise ConfigParseError("no section and no key")
    if root.keys:
        root.end_line = root.keys[-1].line
        sections.insert(0, root)
    doc.sections = sections
    return doc


# ---------------------------------------------------------------------------
# .json — JSONC accepted (vco_lib.jsonc_edit: the ONE JSONC scanner)
# ---------------------------------------------------------------------------

def _json_key_lines(text: str) -> Dict[Tuple[str, ...], int]:
    """Line of the first occurrence of every key path, from the JSONC token
    stream (arrays contribute ``[]``)."""
    from vco_lib.jsonc_edit import tokens

    lines: Dict[Tuple[str, ...], int] = {}
    stack: List[Tuple[str, ...]] = []   # path of each open container
    kinds: List[str] = []               # "{" or "["
    pending: Optional[Tuple[str, ...]] = None
    toks = tokens(text)
    for k, (kind, start, end) in enumerate(toks):
        ch = text[start] if kind == "punct" else ""
        if kind == "str" and kinds and kinds[-1] == "{" and k + 1 < len(toks) \
                and toks[k + 1][0] == "punct" and text[toks[k + 1][1]] == ":":
            path = stack[-1] + (json.loads(text[start:end]),)
            lines.setdefault(path, text.count("\n", 0, start) + 1)
            pending = path
            continue
        if ch in "{[":
            if kinds and kinds[-1] == "[":
                base = stack[-1][:-1] + (stack[-1][-1] + "[]",) if stack[-1] else ("[]",)
            else:
                base = pending if pending is not None else ()
            stack.append(base)
            kinds.append(ch)
            pending = None
        elif ch in "}]":
            if stack:
                stack.pop()
                kinds.pop()
        elif ch == ",":
            pending = None
    return lines


def _json_paths(value: Any, prefix: str, depth: int, out: Dict[str, Any]) -> None:
    """Flatten ``value`` into ``{path: leaf value}`` (insertion order kept).
    Objects descend; arrays of objects descend into the UNION of their keys
    (``a[].k``); arrays of scalars stay one key (rendered as a preview)."""
    if isinstance(value, dict) and depth < JSON_MAX_DEPTH:
        for k, v in value.items():
            _json_paths(v, f"{prefix}.{k}" if prefix else str(k), depth + 1, out)
        if not value and prefix:
            out.setdefault(prefix, value)
        return
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value) \
            and depth < JSON_MAX_DEPTH:
        for item in value:
            _json_paths(item, f"{prefix}[]", depth + 1, out)
        return
    if prefix:
        out.setdefault(prefix, value)


def _path_tuple(section: str, path: str) -> Tuple[str, ...]:
    parts: List[str] = [] if section == ROOT_SECTION else [section]
    for seg in path.split("."):
        parts.append(seg)
    return tuple(parts)


def parse_json(text: str) -> ConfigDoc:
    from vco_lib.jsonc_edit import loads

    try:
        data = loads(text)
        key_lines = _json_key_lines(text)
    except (ValueError, RecursionError) as exc:
        raise ConfigParseError(f"invalid JSON: {exc}") from None
    doc = ConfigDoc(fmt="json")
    last_line = text.count("\n") + 1
    root = ConfigSection(ROOT_SECTION, "$", 1, last_line)
    sections: List[ConfigSection] = []
    if isinstance(data, dict):
        scalars: Dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                flat: Dict[str, Any] = {}
                _json_paths(v, "", 1, flat)
                start = key_lines.get((str(k),), 1)
                sec = ConfigSection(str(k), f"$.{k}", start, start)
                for path, leaf_value in flat.items():
                    line = key_lines.get(_path_tuple(str(k), path), start)
                    sec.keys.append(ConfigKey(path, render_json_value(leaf_value), line))
                    sec.end_line = max(sec.end_line, line)
                sections.append(sec)
            else:
                _json_paths(v, str(k), 1, scalars)
        for path, leaf_value in scalars.items():
            line = key_lines.get(_path_tuple(ROOT_SECTION, path), 1)
            root.keys.append(ConfigKey(path, render_json_value(leaf_value), line))
    elif isinstance(data, list) and data and all(isinstance(v, dict) for v in data):
        flat_root: Dict[str, Any] = {}
        _json_paths(data, "", 0, flat_root)
        for path, leaf_value in flat_root.items():
            line = key_lines.get(_path_tuple(ROOT_SECTION, path), 1)
            root.keys.append(ConfigKey(path, render_json_value(leaf_value), line))
    if root.keys:
        sections.insert(0, root)
    doc.sections = sections
    return doc


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_PARSERS = {"tweakers": parse_tweakers, "ini": parse_ini, "json": parse_json}


def parse_config(fmt: str, text: str, *, key_limit: Optional[int] = None) -> ConfigDoc:
    """Parse ``text`` as ``fmt``; :class:`ConfigParseError` when it is not
    valid. The key cap truncates (``doc.truncated``), it never refuses."""
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ConfigParseError(f"unknown config format {fmt!r}")
    if text.startswith("﻿"):
        text = text[1:]
    return _cap_keys(parser(text), key_limit if key_limit is not None else max_keys())


def skip_reason(content: str, file_path: Path) -> Optional[str]:
    """Why a config file is NOT indexed, or ``None`` when it is. The analyzer's
    shim runs this INSTEAD of the minified-content gate (a one-line JSON config
    is legitimate; the byte cap is this format's size policy)."""
    fmt = config_format(file_path)
    if not fmt:
        return "not a config file"
    size = len(content.encode("utf-8", "surrogatepass"))
    if size > max_bytes():
        return f"config file of {size} bytes is over the {max_bytes()}-byte cap"
    try:
        parse_config(fmt, content)
    except ConfigParseError as exc:
        return f"not valid {fmt} ({exc})"
    return None


def read_config_file(path: Path) -> Optional[ConfigDoc]:
    """Read + parse one file for the link index; ``None`` when it is not
    indexed (wrong name, over the cap, invalid, unreadable). Soft-fails."""
    fmt = config_format(path)
    if not fmt:
        return None
    try:
        if path.stat().st_size > max_bytes():
            return None
        return parse_config(fmt, path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ConfigParseError):
        return None


# ---------------------------------------------------------------------------
# The extractor
# ---------------------------------------------------------------------------

def module_summary(doc: ConfigDoc, rel_path: str) -> str:
    names = [s.name for s in doc.sections]
    keys = [k.path for s in doc.sections for k in s.keys]
    head = f"{doc.fmt.upper()} config {rel_path}" + (f" ({doc.header})" if doc.header else "")
    parts = [f"{head}: sections {', '.join(names[:40])}" if names else f"{head}: no sections"]
    parts.append(f"{doc.total_keys} keys: {', '.join(keys[:40])}")
    if doc.truncated:
        parts.append(f"truncated at {len(keys)} of {doc.total_keys} keys")
    return "; ".join(parts)


def extract_config_file(
    source_text: str, file_path: Path, repo_root: Path, helpers: Any,
) -> FileExtraction:
    """Pure producer: one module row + one section row per section."""
    rel = file_path.relative_to(repo_root).as_posix()
    fmt = config_format(file_path)
    doc = parse_config(fmt, source_text.lstrip("﻿"))
    loc = sum(1 for line in source_text.splitlines() if line.strip())
    module = ModuleDescriptor(
        path=rel, language=fmt, loc=loc, complexity=0.0,
        last_modified=datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc),
        file_hash=hashlib.sha256(source_text.encode()).hexdigest(),
        imports=[], module_summary=module_summary(doc, rel),
    )
    entities: List[CodeEntity] = []
    for section in doc.sections:
        body = "\n".join(body_line(k) for k in section.keys)
        embed_text = "\n".join(p for p in (section.signature, section.doc, body) if p)
        entities.append(CodeEntity(
            kind=KIND_CLASS, file_path_rel=rel,
            name=section.name, full_name=f"{rel}::{section.name}",
            body=body, signature=section.signature, doc=section.doc,
            start_line=section.start_line, end_line=section.end_line,
            project=helpers.project_name,
            extras={"methods": [k.path for k in section.keys], "language": fmt},
            deferred_embed=(lambda t=embed_text: helpers.generate_embedding(t)),
        ))
    return FileExtraction(
        module=module, entities=entities, interactions=[], imports=[],
        stats={"modules": 1, "classes": len(entities)},
    )


def analyze_config_file(ctx: Any, file_path: Path, repo_root: Path) -> Dict[str, int]:
    """Thin shim: the config skip check replaces the minified gate; the
    unchanged-file gate and the writer are the shared ones."""
    from vco_lib.codegraph_lang._shared import run_pure_extractor

    return run_pure_extractor(
        ctx, file_path, repo_root, extract_config_file,
        {"modules": 0, "classes": 0},
        skip_check=skip_reason, skip_stat="config_skipped_invalid", config_links=False,
    )
