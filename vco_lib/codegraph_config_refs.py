# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Config links: code that names a config key → the key (v0.2.105).

The code graph indexes ``.tweakers`` / ``.ini`` / ``.json`` files
(:mod:`vco_lib.codegraph_lang.config`) so a search can go from a code site to
the VALUE it reads, and from a value to every code site that uses it. This
module draws the edges between the two:

* :class:`ConfigIndex` — every config file of ONE source root, parsed from
  disk (never from Weaviate), so a full walk, an incremental walk, an extra
  folder and ``--only-file`` all see the same index. One root's index never
  links into another root (owner ruling: no cross-folder linking).
* :func:`string_literals` — the string literals of a code body, on top of the
  per-language lexer in ``codegraph_lang._shared`` (comments excluded).
* :func:`match_literals` — the precision-first match rules (below).
* :func:`link_groups` — called by the ONE writer
  (``CodeGraphAnalyzer.write_file_extraction``) after a code file's entities
  are written: one ``CodeInteraction`` per (entity, config target), through
  the existing ``_store_interactions`` (``interaction_type == "config"``,
  ``protocol`` = the format, ``endpoint`` = the LOGICAL target, ``raw_target``
  = the literal).
* :func:`link_salt_for` — salts a code file's stored hash with its link set,
  so the unchanged-file gate re-walks exactly the files whose links changed.
* :func:`requeue_on_config_change` — an incremental walk whose range touched a
  config file re-offers the root's code files to that gate.

Endpoints are LOGICAL (format + section + key), not per file: the same key in
four variant folders is one endpoint, so one use writes one link row. The
query side resolves an endpoint to every file that defines it
(:meth:`ConfigIndex.definitions` is that resolution, for a live index).

Match rules (each has an act and a leave-alone test):

1. **Section** — a literal equal to a section name links to
   ``"<fmt>:<section>"``; ``high`` when the name is distinctive (rule 4),
   ``medium`` otherwise; a stoplisted or < 3-character name never links.
   (So a plain section name such as ``"dependencies"`` links ``medium`` from
   every code file that spells it; ``confidence`` marks it as the weak kind.)
2. **Key** — a literal equal to a key (a JSON key's leaf or its full path)
   links to ``"<fmt>:<section>/<key path>"``: ``high`` when that section's
   name or its file's basename ALSO appears as a literal in the same code
   file AND the key passes the rule-1 floor (≥ 3 characters, not
   stoplisted), ``medium`` when the key is distinctive, otherwise no link.
3. **File** — a literal whose basename is an indexed config file's basename
   links to ``"<fmt>:file:<basename>"`` (``high``).
4. **Distinctive** — length ≥ 5, not in :data:`GENERIC_KEYS`, and containing
   ``_``, a space, a digit or a camel hump.
5. **Too generic** — a key that is defined in more than
   :data:`MAX_SECTIONS_PER_KEY` distinct sections gets no link.
6. **Dedup** — one link per (entity, endpoint); the interaction identity
   ``ix::<entity>::<endpoint>`` makes re-runs replace rows, never duplicate.

An unlinked key is a legitimate result (stale, or its name is built at run
time), never an error.
"""
from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, cast

from vco_lib.codegraph_lang.config import (
    CONFIG_DISPATCH_KEY,
    CONFIG_FORMATS,
    ConfigDoc,
    config_format,
    read_config_file,
)

logger = logging.getLogger(__name__)

INTERACTION_TYPE = "config"
#: Rule 5: a key defined in more sections than this is too generic to link.
MAX_SECTIONS_PER_KEY = 5
#: Literals longer than this are prose, not names.
MAX_LITERAL_CHARS = 200
#: Rule 4 stoplist: generic key names that match by accident.
GENERIC_KEYS = frozenset({
    "name", "type", "id", "value", "values", "version", "width", "height",
    "enabled", "disabled", "path", "data", "default", "description", "title",
    "label", "key", "keys", "items", "item", "size", "count", "index", "mode",
    "text", "color", "url", "file", "files", "list", "config", "settings",
    "options", "params", "true", "false", "null", "none", "error", "message",
    "status", "state", "result", "results", "source", "target", "level",
    "x", "y", "z",
})

_CAMEL_RE = re.compile(r"[a-z][A-Z]")


def _plain_name_ok(name: str) -> bool:
    """The floor every NON-distinctive name must pass to link at all (a
    section name, or a key upgraded by co-occurrence): ≥ 3 characters and not
    in :data:`GENERIC_KEYS`. ``"x"`` or ``"name"`` never link, whatever
    co-occurs with them."""
    return len(name) >= 3 and name.lower() not in GENERIC_KEYS


def is_distinctive(name: str) -> bool:
    """Rule 4."""
    if len(name) < 5 or name.lower() in GENERIC_KEYS:
        return False
    return ("_" in name or " " in name or any(c.isdigit() for c in name)
            or _CAMEL_RE.search(name) is not None)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyDef:
    fmt: str
    rel_path: str
    section: str
    path: str
    line: int
    value: str
    description: str = ""
    default: bool = False

    @property
    def endpoint(self) -> str:
        return key_endpoint(self.fmt, self.section, self.path)


@dataclass(frozen=True)
class SectionDef:
    fmt: str
    rel_path: str
    name: str
    line: int


def key_endpoint(fmt: str, section: str, path: str) -> str:
    return f"{fmt}:{section}/{path}"


def section_endpoint(fmt: str, section: str) -> str:
    return f"{fmt}:{section}"


def file_endpoint(fmt: str, basename: str) -> str:
    return f"{fmt}:file:{basename}"


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _lookup_names(fmt: str, key_path: str) -> List[str]:
    """The literals a key can be named by: its whole path, plus (JSON) its
    leaf. INI and ``.tweakers`` keys may contain dots — never split them."""
    names = [key_path]
    if fmt == "json":
        leaf = key_path.rsplit(".", 1)[-1].replace("[]", "")
        if leaf and leaf != key_path:
            names.append(leaf)
    return names


@dataclass
class ConfigIndex:
    """Every config key, section and file of ONE source root."""

    keys: Dict[str, List[KeyDef]] = field(default_factory=dict)
    sections: Dict[str, List[SectionDef]] = field(default_factory=dict)
    files: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)

    def add(self, rel_path: str, doc: ConfigDoc) -> None:
        self.files.setdefault(_basename(rel_path), []).append((doc.fmt, rel_path))
        for section in doc.sections:
            self.sections.setdefault(section.name, []).append(
                SectionDef(doc.fmt, rel_path, section.name, section.start_line))
            for key in section.keys:
                kd = KeyDef(doc.fmt, rel_path, section.name, key.path, key.line,
                            key.value, key.description, key.default)
                for name in _lookup_names(doc.fmt, key.path):
                    self.keys.setdefault(name, []).append(kd)

    @property
    def empty(self) -> bool:
        return not self.files

    def names(self) -> Set[str]:
        """Every literal that could match something (the cheap pre-filter)."""
        return set(self.keys) | set(self.sections) | set(self.files)

    def definitions(self, endpoint: str) -> List[KeyDef]:
        """Every place a key endpoint is defined — with its VALUE. The seam a
        ``config_users`` query resolves a logical endpoint through."""
        return sorted(
            {kd for defs in self.keys.values() for kd in defs if kd.endpoint == endpoint},
            key=lambda kd: (kd.rel_path, kd.line),
        )


def build_index(root: Path, files: Iterable[Path]) -> ConfigIndex:
    """Parse ``files`` (paths under ``root``) into a :class:`ConfigIndex`.
    A file that is not indexable is left out; nothing raises."""
    index = ConfigIndex()
    for path in sorted(files):
        doc = read_config_file(path)
        if doc is None:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        index.add(rel, doc)
    return index


def config_index_for(ctx: Any, root: Optional[Path]) -> Optional[ConfigIndex]:
    """The walk-wide index of ``root``, built once per analyzer run from the
    walk's OWN finder (``ctx._find_files_for("config", root)`` — same ignore
    set, so ``bin/`` stays out). An extra folder never contributes its
    ``.claude/`` (the walk excludes it there). ``None`` when the analyzer
    cannot provide one (a test stub) — linking then does nothing."""
    find = getattr(ctx, "_find_files_for", None)
    if root is None or not callable(find):
        return None
    cache: Dict[str, Optional[ConfigIndex]] = ctx.__dict__.setdefault("_config_indexes", {})
    key = str(root)
    if key not in cache:
        try:
            files: List[Path] = list(cast(Iterable[Path], find(CONFIG_DISPATCH_KEY, root)))
            primary = getattr(ctx, "_analyze_repo_root", None)
            if primary is not None and Path(root).resolve() != primary:
                files = [f for f in files if ".claude" not in f.relative_to(root).parts]
            cache[key] = build_index(Path(root), files)
        except Exception as exc:  # noqa: BLE001 — linking is best-effort, never fatal
            logger.warning("config index for %s unavailable (config links of this "
                           "walk's code files are left as they are): %s", root, exc)
            cache[key] = None
            ctx.__dict__.setdefault("_config_index_failed", set()).add(key)
    return cache[key]


def index_build_failed(ctx: Any, root: Optional[Path]) -> bool:
    """Did :func:`config_index_for` TRY to build ``root``'s index and fail?
    (``None`` from it also means "no finder" — a stub, not a failure.)"""
    return root is not None and str(root) in (ctx.__dict__.get("_config_index_failed") or ())


def _withhold_link_judgment(ctx: Any) -> None:
    """M1 (v0.2.105 fix round): linking FAILED for the file being written, so
    its zero links are no evidence its old link rows are gone — the end-of-walk
    reconcile must not judge this file's ``CodeInteraction`` rows."""
    from vco_lib.codegraph_resync import withhold_file_judgment

    coll = getattr(ctx, "interactions_collection", None)
    withhold_file_judgment(ctx, str(getattr(coll, "name", "") or ""))


# ---------------------------------------------------------------------------
# String literals
# ---------------------------------------------------------------------------

_LITERAL_RES: Dict[str, "re.Pattern[str]"] = {}


def _literal_re(language: str, single_quotes: bool) -> "re.Pattern[str]":
    key = f"{language}:{single_quotes}"
    pat = _LITERAL_RES.get(key)
    if pat is None:
        parts = []
        if language == "cpp":
            parts.append(r'R"(?P<delim>[^()\\ \t]{0,16})\((?P<raw>.*?)\)(?P=delim)"')
        if language == "lua":
            parts.append(r"\[(?P<eq>=*)\[(?P<long>.*?)\](?P=eq)\]")
        parts.append(r'"(?P<dq>(?:[^"\\\n]|\\.)*)"')
        if single_quotes:
            parts.append(r"'(?P<sq>(?:[^'\\\n]|\\.)*)'")
        pat = _LITERAL_RES[key] = re.compile("|".join(parts))
    return pat


def _literals_in_line(line: str, language: str, single_quotes: bool) -> List[str]:
    out: List[str] = []
    for m in _literal_re(language, single_quotes).finditer(line):
        groups = m.groupdict()
        raw = next((groups[g] for g in ("raw", "long", "dq", "sq") if groups.get(g) is not None), "")
        text = re.sub(r"\\(.)", r"\1", raw).strip()
        if text and len(text) <= MAX_LITERAL_CHARS:
            out.append(text)
    return out


def string_literals(language: str, text: str) -> List[str]:
    """The string literals of ``text`` in source order, comments excluded.

    Built on the per-language lexer of ``codegraph_lang._shared`` (C/C++ raw
    strings, Lua long brackets, Python triple quotes, C# verbatim strings,
    JS/TS, Go, Rust, Java, shell, PowerShell): the lexer removes comments and
    keeps strings verbatim, and the literals are read off the kept text. The
    body of a string that spans lines is skipped — names are one-line
    literals."""
    from vco_lib.codegraph_lang._shared import (
        _carry_state,
        _scan_construct,
        _scrub_line_stateful,
        _syntax_for,
    )

    syn = _syntax_for(language)
    out: List[str] = []
    state = None
    for line in text.splitlines():
        if state is not None:
            i, rest = _scan_construct(line, 0, state)
            if rest is not None:
                state = _carry_state(rest, line)
                continue
            line = line[i:]
        kept, state = _scrub_line_stateful(line, syn, None, keep_strings=True)
        out.extend(_literals_in_line(kept, language, syn.single_quote_string))
    return out


_QUICK_RE = re.compile(r"""["']([^"'\\\n]{1,200})["']|\[=*\[([^\]\n]{1,200})\]""")


def _quick_candidates(text: str) -> Set[str]:
    """A cheap, lexer-free superset-ish of the one-line literals (pre-filter)."""
    out: Set[str] = set()
    for m in _QUICK_RE.finditer(text):
        val = (m.group(1) or m.group(2) or "").strip()
        if val:
            out.add(val)
            out.add(_basename(val))
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _link(fmt: str, endpoint: str, literal: str, confidence: str, source: str) -> Dict[str, str]:
    return {
        "interaction_type": INTERACTION_TYPE, "direction": "outbound",
        "protocol": fmt, "endpoint": endpoint, "raw_target": literal,
        "confidence": confidence, "source": source,
    }


def match_literals(
    literals: Iterable[str],
    file_literals: Set[str],
    index: ConfigIndex,
    *,
    source: str,
) -> List[Dict[str, str]]:
    """Rules 1-6 for one entity's literals. ``file_literals`` is every literal
    of the whole code file (rule 2's co-occurrence)."""
    found: Dict[str, Dict[str, str]] = {}

    def add(fmt: str, endpoint: str, literal: str, confidence: str) -> None:
        prev = found.get(endpoint)
        if prev is None or (prev["confidence"] != "high" and confidence == "high"):
            found[endpoint] = _link(fmt, endpoint, literal, confidence, source)

    file_bases = {_basename(lit) for lit in file_literals}
    for lit in sorted(set(literals)):
        base = _basename(lit)
        for fmt, _rel in index.files.get(base, ()):            # rule 3
            add(fmt, file_endpoint(fmt, base), lit, "high")
        plain = _plain_name_ok(lit)
        secs = index.sections.get(lit)
        if secs and plain:                                       # rule 1
            distinct = sorted({s.fmt for s in secs})
            conf = "high" if is_distinctive(lit) else "medium"
            for fmt in distinct:
                add(fmt, section_endpoint(fmt, lit), lit, conf)
        defs = index.keys.get(lit)
        if not defs:
            continue
        sections = {(d.fmt, d.section) for d in defs}
        if len(sections) > MAX_SECTIONS_PER_KEY:                 # rule 5
            logger.debug("config key %r is in %d sections — too generic to link",
                         lit, len(sections))
            continue
        by_endpoint: Dict[str, List[KeyDef]] = {}
        for d in defs:
            by_endpoint.setdefault(d.endpoint, []).append(d)
        for endpoint, group in sorted(by_endpoint.items()):    # rule 2
            cooc = any(d.section in file_literals or _basename(d.rel_path) in file_bases
                       for d in group)
            if cooc and plain:
                add(group[0].fmt, endpoint, lit, "high")
            elif is_distinctive(lit):
                add(group[0].fmt, endpoint, lit, "medium")
    return [found[k] for k in sorted(found)]


# ---------------------------------------------------------------------------
# Writer-side: the link groups for one written file
# ---------------------------------------------------------------------------

def _language_key(ctx: Any) -> str:
    return str(getattr(ctx, "_current_language", "") or "")


def link_groups(ctx: Any, fx: Any, written: Sequence[Tuple[Any, str]]) -> List[Any]:
    """The config-link ``InteractionGroup`` s of one written code file.

    ``written`` is the writer's ``[(entity, uuid)]`` in write order. Each
    FUNCTION entity's literals link with ``source_function`` = its uuid; the
    literals of top-level code (outside every function) link with the module
    only. Empty for a config file, a stub analyzer, or a root with no config
    files. Soft-fails to ``[]``; on a FAILURE (the index could not be built,
    or linking raised) it also withholds the end-of-walk reconcile's judgement
    of this file's interaction rows, so a failure never deletes old links."""
    from vco_lib.codegraph_entities import InteractionGroup, KIND_FUNCTION

    language = _language_key(ctx)
    md = getattr(fx, "module", None)
    if md is None or language == CONFIG_DISPATCH_KEY or md.language in CONFIG_FORMATS.values():
        return []
    root = getattr(fx, "source_root", None)
    try:
        index = config_index_for(ctx, root)
        if index is None and index_build_failed(ctx, root):
            _withhold_link_judgment(ctx)
        if index is None or index.empty:
            return []
        text = getattr(fx, "source_text", None) or ""
        if not (_quick_candidates(text) & index.names()):
            return []
        file_literals = set(string_literals(language, text))
        if not (file_literals & index.names() or {_basename(x) for x in file_literals} & set(index.files)):
            return []
        groups: List[Any] = []
        in_functions: Set[str] = set()
        for entity, uuid in written:
            if entity.kind != KIND_FUNCTION or not entity.body:
                continue
            lits = set(string_literals(language, entity.body))
            in_functions |= lits
            links = match_literals(lits, file_literals, index,
                                   source=entity.full_name or entity.name or "")
            if links:
                groups.append(InteractionGroup(interactions=links, language=md.language,
                                               func_uuid=uuid))
        top_level = file_literals - in_functions
        links = match_literals(top_level, file_literals, index, source=md.path)
        if links:
            groups.append(InteractionGroup(interactions=links, language=md.language))
        return groups
    except Exception as exc:  # noqa: BLE001 — a link failure never fails the file
        logger.warning("config links for %s skipped (its existing link rows are "
                       "kept): %s", getattr(md, "path", "?"), exc)
        _withhold_link_judgment(ctx)
        return []


def link_salt(language: str, content: str, index: Optional[ConfigIndex]) -> str:
    """A fingerprint of the file's config links (``""`` when it has none).

    Salted into the code file's stored hash: a key added to, moved in or
    removed from a config file changes the fingerprint of exactly the code
    files that name it, so the unchanged-file gate re-walks (and relinks)
    those and only those. Values are NOT part of it — a value lives on the
    config file's own section row, which re-walks when the config file
    changes."""
    if index is None or index.empty or language == CONFIG_DISPATCH_KEY:
        return ""
    if not (_quick_candidates(content) & index.names()):
        return ""
    lits = set(string_literals(language, content))
    links = match_literals(lits, lits, index, source="")
    if not links:
        return ""
    payload = "\n".join(f"{x['endpoint']}|{x['confidence']}|{x['raw_target']}" for x in links)
    return "config-links:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def link_salt_for(ctx: Any, root: Path, content: str) -> str:
    """:func:`link_salt` for the file the analyzer is about to gate."""
    try:
        return link_salt(_language_key(ctx), content, config_index_for(ctx, root))
    except Exception as exc:  # noqa: BLE001 — no salt rather than a failed file
        logger.debug("config link salt skipped: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Incremental re-queue
# ---------------------------------------------------------------------------

def config_changed_in_range(
    ctx: Any, root: Path, since_commit: Optional[str], *,
    run: Callable[..., Any] = subprocess.run,
) -> bool:
    """Did ``<since_commit or HEAD~1>..HEAD`` add, change or delete a config
    file under ``root``? Same range rule as the changed-files filter and the
    deleted-file prune (one diff, three readers). Cached per root."""
    from vco_lib.codegraph_deleted_files import filter_changed_files, git_deleted_paths

    cache: Dict[str, bool] = ctx.__dict__.setdefault("_config_changed", {})
    key = str(root)
    if key not in cache:
        try:
            files = ctx._find_files_for(CONFIG_DISPATCH_KEY, root)
            changed = bool(files and filter_changed_files(root, files, since_commit, run=run))
            if not changed:
                gone = git_deleted_paths(root, since_commit, run=run)
                changed = any(config_format(p) for p in gone.paths)
        except Exception as exc:  # noqa: BLE001 — unknown ⇒ no extra re-queue
            logger.debug("config change probe for %s failed: %s", root, exc)
            changed = False
        cache[key] = changed
    return cache[key]


def requeue_on_config_change(
    ctx: Any, root: Path, lang: str, all_files: List[Path], files: List[Path],
    since_commit: Optional[str], *, run: Callable[..., Any] = subprocess.run,
) -> List[Path]:
    """Incremental walk: when the range touched a config file under ``root``,
    offer EVERY ``lang`` file of the root to the unchanged-file gate. The
    gate's hash carries :func:`link_salt`, so only the files whose links
    changed re-walk; the rest hash-skip. A ``--only-file`` run never comes
    through here (its config edit relinks on the next incremental walk)."""
    if lang == CONFIG_DISPATCH_KEY or len(files) == len(all_files):
        return files
    if not config_changed_in_range(ctx, root, since_commit, run=run):
        return files
    extra = [f for f in all_files if f not in set(files)]
    if extra:
        print(f"ℹ️  A config file changed under {root}: re-checking {len(extra)} "
              f"more {lang} file(s) for config links (unchanged links hash-skip)")
    return list(files) + extra
