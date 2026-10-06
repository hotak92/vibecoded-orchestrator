# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Open KG node-type + knowledge-folder vocabulary — the SSOT parser.

**SSOT classification (v0.2.91, task #33)**: this module is the single
source of truth for the OPEN node-type and knowledge-subfolder vocabulary.
A project extends the shipped built-ins by declaring additional classes in
its ``knowledge/VOCABULARY.md`` ontology; this parser turns that file into:

* an open **node-type set** — the nine built-ins ∪ every declared alias;
* an open **folder registry** — the built-in ``knowledge/`` subfolders ∪
  every explicitly declared folder;
* an open **node_type → folder mapping** — the built-in mapping ∪ the
  declared custom routes.

Consumers (keep this list current):

* ``claude_mcp_servers/weaviate_mcp/server.py`` — ``_normalize_kg_file_path``
  trusts declared subfolders like built-in ones. Its module-level
  ``_KNOWLEDGE_SUBFOLDERS`` / ``_NODE_TYPE_TO_FOLDER`` literals are the
  built-in base of this open set and MUST match the ``BUILTIN_*`` constants
  below (pinned by ``tests/test_v0291_kg_vocabulary_consumers.py``).
  ``store_knowledge_node`` applies the shared node-type gate
  (:func:`classify_node_type`) and auto-extends through
  :func:`extend_vocabulary` (v0.2.101, P299-A3).
* ``templates/scripts/sync_knowledge_graph.py`` — the node validator
  delegates here (A-leg); its inline ImportError-fallback parser MUST match
  ``parse_vocabulary_text``'s type extraction (same parity test).
  ``sync_node`` applies the shared node-type gate and auto-extends through
  :func:`extend_vocabulary` (v0.2.101, P299-A3).
* ``vco_lib/kg_sync_drift.py`` — the drift scanner's skip predicate calls
  :func:`is_wellformed_type` so it skips EXACTLY the nodes ``sync_node``
  fails loudly for (an invalid-type node can never reach Weaviate;
  reporting it as drift would be a permanent phantom).

The node-type GATE (v0.2.101, P299-A3) — one shared decision for every KG
write path, born from the 2026-09 incident where a project's sync held 377
of 546 disk nodes because a per-path validator REJECTED every undeclared
``type:``:

* :func:`classify_node_type` → ``known`` / ``extendable`` / ``invalid``;
* ``extendable`` (wellformed but undeclared) is INGESTED and the type is
  auto-declared via :func:`extend_vocabulary` — the vocabulary is OPEN by
  design, so an unknown type grows the file instead of dropping the node;
* ``invalid`` (empty/malformed) is refused LOUDLY by the caller (a failed
  sync outcome / a store error payload) — never stored, never silently
  skipped.

Declaration format (the shape ``templates/knowledge/VOCABULARY.md`` already
uses for the built-ins — see its "Declaring your own node types" section):

    #### **`co:Thought`** (alias: `thought`)
    - **Definition**: A fleeting idea captured before it is lost
    - **Folder**: `thoughts`

* The class heading MUST be a markdown heading line of the exact shape
  ``#### **`co:Name`** (alias: `name`)``. The alias becomes the node type
  (lowercased). Relationship sections (``#### **`uses`** (co:uses)``) never
  match — they carry no ``co:`` bold name and no ``alias:``.
* The OPTIONAL ``- **Folder**: `name``` bullet inside the class section
  declares a dedicated ``knowledge/`` subfolder for the type (single path
  segment, ``[A-Za-z0-9_-]+``). It both routes the type there and registers
  the folder as trusted for path normalization.
* Without a Folder line a custom type files under ``knowledge/concepts/`` —
  the same default the built-in ``pattern`` / ``insight`` / ``guide`` types
  use (they have no dedicated folder in the built-in mapping either).
* A declaration can NOT re-route a BUILT-IN type to a different folder —
  the built-in mapping wins and a warning is recorded (existing on-disk
  layouts must not silently migrate).

Anti-fooling guarantees (see the source-text-gates lesson — a parser
satisfiable by a DESCRIPTION of a declaration fails toward green):

* Only real heading LINES declare types — ``alias: `x``` inside prose,
  tables, or bullet text never matches.
* Fenced code blocks (``````` / ``~~~``) are skipped entirely, so the
  documentation examples inside VOCABULARY.md itself are inert.

Capacity note: the RL reranker's type-embedding registry is claimed to
support 256+ categories, but the registry lives in the private RL module
and is NOT verifiable from this repository — free-tier retrieval treats
``node_type`` as an opaque string (no cap). ``parse_vocabulary_text``
records a soft warning when the total type set exceeds
``RL_TYPE_CAPACITY_SOFT_CAP`` so users can verify against their RL
module's actual capacity.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Union

from vco_lib.atomic import LockTimeout, exclusive_file_lock

__all__ = [
    "BUILTIN_NODE_TYPES",
    "BUILTIN_KNOWLEDGE_SUBFOLDERS",
    "BUILTIN_NODE_TYPE_TO_FOLDER",
    "DEFAULT_NODE_FOLDER",
    "RL_TYPE_CAPACITY_SOFT_CAP",
    "TYPE_KNOWN",
    "TYPE_EXTENDABLE",
    "TYPE_INVALID",
    "AUTO_SECTION_MARKER",
    "KgVocabulary",
    "VocabularyExtension",
    "builtin_vocabulary",
    "parse_vocabulary_text",
    "load_vocabulary",
    "clear_vocabulary_cache",
    "is_wellformed_type",
    "classify_node_type",
    "extend_vocabulary",
]

# ── Built-ins (behavior-preserving base of the open set) ─────────────────────
#
# The nine node types shipped with every project — one per class declared in
# the shipped templates/knowledge/VOCABULARY.md.
BUILTIN_NODE_TYPES: frozenset[str] = frozenset({
    "project", "concept", "tool", "research", "model", "hardware",
    "pattern", "insight", "guide",
})

# The knowledge/ subfolders trusted verbatim by path normalization.
# Historically the closed set in weaviate_mcp/server.py::_KNOWLEDGE_SUBFOLDERS
# — preserved byte-for-byte; the consumer's literal must match (parity test).
BUILTIN_KNOWLEDGE_SUBFOLDERS: frozenset[str] = frozenset({
    "concepts", "coordination", "hardware", "insights", "models", "notes",
    "patterns", "projects", "research", "techniques", "tools", "training", "user",
})

# Canonical node_type → knowledge subfolder mapping (historically the closed
# dict in weaviate_mcp/server.py::_NODE_TYPE_TO_FOLDER — preserved verbatim).
# Types absent here (pattern / insight / guide / customs without a Folder
# line) default to DEFAULT_NODE_FOLDER.
BUILTIN_NODE_TYPE_TO_FOLDER: Mapping[str, str] = MappingProxyType({
    "project":       "projects",
    "concept":       "concepts",
    "tool":          "tools",
    "model":         "models",
    "hardware":      "hardware",
    "research":      "research",
    "coordination":  "coordination",
})

DEFAULT_NODE_FOLDER = "concepts"

#: Soft cap for the total node-type set. The RL reranker's type registry is
#: claimed to handle 256+ categories, but that registry is in the private RL
#: module (unverifiable here) — exceeding this only records a warning.
RL_TYPE_CAPACITY_SOFT_CAP = 256


# ── Parsed vocabulary ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KgVocabulary:
    """A project's resolved (open) KG vocabulary.

    ``warnings`` carries non-fatal parse findings (built-in re-route
    attempts, capacity soft-cap, unreadable-file notes) — callers surface
    them at their own severity; nothing here is a hard error.
    """

    node_types: frozenset[str] = BUILTIN_NODE_TYPES
    knowledge_subfolders: frozenset[str] = BUILTIN_KNOWLEDGE_SUBFOLDERS
    # default_factory: a mappingproxy is unhashable, which dataclasses
    # rejects as a plain default.
    node_type_to_folder: Mapping[str, str] = field(
        default_factory=lambda: BUILTIN_NODE_TYPE_TO_FOLDER
    )
    warnings: tuple[str, ...] = ()

    def folder_for(self, node_type: str) -> str:
        """Canonical knowledge/ subfolder for *node_type* (open mapping)."""
        return self.node_type_to_folder.get(node_type, DEFAULT_NODE_FOLDER)


def builtin_vocabulary() -> KgVocabulary:
    """The built-ins-only vocabulary (no VOCABULARY.md contribution)."""
    return KgVocabulary()


# ── Parser ───────────────────────────────────────────────────────────────────
#
# Class heading — the REAL shape the shipped VOCABULARY.md uses, anchored to a
# heading line so prose/tables mentioning ``alias: `x``` can never declare a
# type. Relationship headings (``#### **`uses`** (co:uses)``) don't match:
# no ``co:`` inside the bold backticks, no ``alias:``.
_CLASS_HEADING_RE = re.compile(
    r"^\s{0,3}#{1,6}\s+\*\*`co:(?P<name>[A-Za-z0-9_-]+)`\*\*\s*"
    r"\(alias:\s*`(?P<alias>[A-Za-z0-9_-]+)`\)\s*$"
)
# Any ATX heading — closes the current class section.
_ANY_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
# Optional folder declaration bullet inside a class section. Single path
# segment only (charset forbids separators / dots) — a folder value can never
# escape knowledge/.
_FOLDER_LINE_RE = re.compile(
    r"^\s*-\s*\*\*Folder\*\*\s*:\s*`(?P<folder>[A-Za-z0-9_-]+)`\s*$",
    re.IGNORECASE,
)
# Code-fence delimiter (``` or ~~~) — everything inside a fence is inert.
_FENCE_RE = re.compile(r"^\s{0,3}(?P<delim>```|~~~)")


def parse_vocabulary_text(text: str) -> KgVocabulary:
    """Parse VOCABULARY.md *text* into the open vocabulary (pure function).

    Never raises on malformed content — unrecognized lines are simply not
    declarations. See the module docstring for the declaration format.
    """
    types: set[str] = set(BUILTIN_NODE_TYPES)
    subfolders: set[str] = set(BUILTIN_KNOWLEDGE_SUBFOLDERS)
    type_to_folder: dict[str, str] = dict(BUILTIN_NODE_TYPE_TO_FOLDER)
    warnings: list[str] = []

    fence_delim: Optional[str] = None  # inside a code fence when not None
    current_alias: Optional[str] = None  # class section currently open

    for line in text.splitlines():
        fence_m = _FENCE_RE.match(line)
        if fence_m:
            delim = fence_m.group("delim")
            if fence_delim is None:
                fence_delim = delim  # opening fence
            elif delim == fence_delim:
                fence_delim = None  # matching closing fence
            continue
        if fence_delim is not None:
            continue  # inside a fence — documentation example, inert

        class_m = _CLASS_HEADING_RE.match(line)
        if class_m:
            # The alias group is non-optional in _CLASS_HEADING_RE, so a match
            # always carries it — str() is a type-level fact for pyright (which
            # models Match.group as possibly-None), not a runtime guard.
            current_alias = str(class_m.group("alias")).lower()
            types.add(current_alias)
            continue
        if _ANY_HEADING_RE.match(line):
            current_alias = None  # any other heading ends the class section
            continue

        if current_alias is not None:
            folder_m = _FOLDER_LINE_RE.match(line)
            if folder_m:
                # Same type-level fact as the alias group above.
                folder = str(folder_m.group("folder"))
                subfolders.add(folder)
                if current_alias in BUILTIN_NODE_TYPE_TO_FOLDER:
                    builtin_folder = BUILTIN_NODE_TYPE_TO_FOLDER[current_alias]
                    if folder != builtin_folder:
                        warnings.append(
                            f"VOCABULARY.md declares folder '{folder}' for "
                            f"built-in type '{current_alias}' — built-in "
                            f"routing to '{builtin_folder}' is preserved "
                            f"(built-ins cannot be re-routed)."
                        )
                elif current_alias in BUILTIN_NODE_TYPES:
                    # Built-in without a dedicated folder (pattern/insight/
                    # guide): same preservation rule — default stays.
                    warnings.append(
                        f"VOCABULARY.md declares folder '{folder}' for "
                        f"built-in type '{current_alias}' — built-in default "
                        f"'{DEFAULT_NODE_FOLDER}' is preserved."
                    )
                else:
                    type_to_folder[current_alias] = folder

    if len(types) > RL_TYPE_CAPACITY_SOFT_CAP:
        warnings.append(
            f"Vocabulary declares {len(types)} node types (> "
            f"{RL_TYPE_CAPACITY_SOFT_CAP}) — verify against your RL "
            f"module's type-embedding capacity before relying on RL "
            f"reranking for all of them."
        )

    return KgVocabulary(
        node_types=frozenset(types),
        knowledge_subfolders=frozenset(subfolders),
        node_type_to_folder=MappingProxyType(type_to_folder),
        warnings=tuple(warnings),
    )


# ── Cached loader ────────────────────────────────────────────────────────────
#
# Freshness token for an entry: the file's ``st_mtime_ns`` when it exists,
# ``None`` when it does not (or its metadata is unreadable). Every call
# re-stats the file — a cheap existence/freshness check — so a LONG-LIVED
# process (the weaviate-kg MCP) picks up mid-session edits automatically:
# the driving use case is "declare a type in VOCABULARY.md, then
# immediately write a node of that type" within one session. One entry per
# path (replaced on token change), so the cache stays bounded.
_MISSING_TOKEN = None
_CACHE: dict[str, tuple[Optional[int], KgVocabulary]] = {}


def _freshness_token(vocab_path: Path) -> Optional[int]:
    """``st_mtime_ns`` of the file, or ``_MISSING_TOKEN`` when absent /
    unstatable. A missing file caches under the sentinel; the per-call
    stat re-checks existence, so the missing→created transition is picked
    up on the very next load (the new mtime_ns mismatches the sentinel)."""
    try:
        return vocab_path.stat().st_mtime_ns
    except OSError:
        return _MISSING_TOKEN


def load_vocabulary(
    project_root: Union[str, Path],
    *,
    use_cache: bool = True,
) -> KgVocabulary:
    """Load ``<project_root>/knowledge/VOCABULARY.md`` as an open vocabulary.

    Missing or unreadable file (``OSError`` — permissions, transient FS —
    AND ``UnicodeDecodeError`` — binary/mis-encoded content) → built-ins
    only, never a crash; non-missing read failures are recorded in
    ``warnings``.

    Results are cached per-process, keyed by the resolved VOCABULARY.md
    path + the file's ``st_mtime_ns`` (see ``_freshness_token``): editing
    or creating the file invalidates automatically on the next call, even
    in a long-lived consumer like the MCP server. Pass ``use_cache=False``
    to bypass the lookup AND refresh the entry regardless of mtime (the
    hatch for a same-mtime content change — sub-granularity filesystems,
    deliberate ``utime`` resets); ``clear_vocabulary_cache()`` drops all
    entries.
    """
    vocab_path = Path(project_root) / "knowledge" / "VOCABULARY.md"
    try:
        cache_key = str(vocab_path.resolve())
    except OSError:  # pathological root (e.g. dangling cwd) — still degrade
        cache_key = str(vocab_path)

    token = _freshness_token(vocab_path)
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached is not None and cached[0] == token:
            return cached[1]

    try:
        text = vocab_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        vocab = builtin_vocabulary()  # normal for projects without an ontology
    except (OSError, UnicodeDecodeError) as exc:
        vocab = KgVocabulary(
            warnings=(
                f"knowledge/VOCABULARY.md unreadable ({exc.__class__.__name__}: "
                f"{exc}) — using built-in vocabulary only.",
            )
        )
    else:
        vocab = parse_vocabulary_text(text)

    _CACHE[cache_key] = (token, vocab)
    return vocab


def clear_vocabulary_cache() -> None:
    """Drop every cached vocabulary (tests / long-lived processes)."""
    _CACHE.clear()


# ── The node-type gate — ONE shared decision (v0.2.101, P299-A3) ─────────────
#
# Every KG write path classifies a node's ``type:`` value with the SAME rule
# (see the module docstring): kg-sync's ``sync_node``, the weaviate-kg MCP's
# ``store_knowledge_node``, and the drift scanner's skip predicate. The
# 2026-09 incident this closes: one path's validator rejecting what the
# others accepted dropped 169 of 546 nodes from a project's collection.

#: The alias charset of :data:`_CLASS_HEADING_RE` — a type value that cannot
#: be spelled in this charset can never be declared, so it is INVALID
#: (reported loudly by the callers, never stored, never auto-extended).
_TYPE_CHARSET_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Gate verdicts returned by :func:`classify_node_type`.
TYPE_KNOWN = "known"            # declared (built-in or VOCABULARY.md alias)
TYPE_EXTENDABLE = "extendable"  # wellformed but undeclared → auto-declare
TYPE_INVALID = "invalid"        # empty/malformed → refuse loudly


def is_wellformed_type(node_type: object) -> bool:
    """True when *node_type* could become a vocabulary alias: a non-empty
    ``[A-Za-z0-9_-]+`` string (surrounding whitespace ignored).

    This is the SHARED invalid/valid boundary — ``sync_node`` fails a node
    loudly exactly when this returns False for its frontmatter-declared
    ``type:``, and ``kg_sync_drift`` skips exactly those nodes (parity
    pinned by ``tests/test_v02101_kg_vocabulary_autoextend.py``).
    """
    return isinstance(node_type, str) and bool(
        _TYPE_CHARSET_RE.match(node_type.strip())
    )


def classify_node_type(node_type: object, vocab: KgVocabulary) -> str:
    """The one gate decision for a node's ``type:`` value.

    Returns :data:`TYPE_KNOWN` (declared — case-insensitive, because the
    declaration parser lowercases every alias, so ``Concept`` and
    ``concept`` are the same type), :data:`TYPE_EXTENDABLE` (wellformed
    but undeclared — ingest and auto-declare), or :data:`TYPE_INVALID`
    (empty/malformed — refuse loudly, never store).
    """
    if not is_wellformed_type(node_type):
        return TYPE_INVALID
    alias = str(node_type).strip().lower()
    if alias in vocab.node_types:
        return TYPE_KNOWN
    return TYPE_EXTENDABLE


# ── Auto-extend (append-only) ────────────────────────────────────────────────

#: Marker of the auto-extended section :func:`extend_vocabulary` maintains.
#: An HTML comment so it is inert for every markdown renderer AND for the
#: declaration parser (only ``#### **`co:…`** (alias: `…`)`` headings count).
AUTO_SECTION_MARKER = "<!-- vco-auto-extended-types -->"

_AUTO_SECTION_HEADING = """\
## Auto-extended node types

Declared automatically by VCO when a node used an undeclared, wellformed
`type:` value — the node-type vocabulary is OPEN, so kg-sync and the
weaviate-kg MCP grow this file instead of rejecting the node (v0.2.101,
P299-A3). Edit the definitions freely; keep the heading shape so the
parser sees them. A type declared here files under `knowledge/concepts/`
unless you add a `- **Folder**: `name`` bullet (see "Declaring your own
node types" above).
"""

_NEW_FILE_HEADER = """\
# Knowledge Graph Vocabulary

This project's KG node-type vocabulary. The built-in types (project,
concept, tool, research, model, hardware, pattern, insight, guide) are
always available even when this file does not declare them. This file was
created automatically by VCO to declare auto-extended types; see the
orchestrator's `templates/knowledge/VOCABULARY.md` for the full shipped
ontology and the declaration format.

"""


@dataclass(frozen=True)
class VocabularyExtension:
    """Outcome of one :func:`extend_vocabulary` call.

    ``added`` carries the lowercased aliases appended; ``already_declared``
    the inputs that needed no write; ``invalid`` the inputs rejected by
    :func:`is_wellformed_type` (repr-ed for non-strings). ``error`` is
    non-empty when the file could not be written — a SOFT failure the
    caller must report loudly (the node still syncs; the type simply stays
    undeclared and the next run retries).
    """

    added: tuple[str, ...] = ()
    already_declared: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()
    vocabulary_path: str = ""
    error: str = ""


def _pascal_class_name(alias: str) -> str:
    """Canonical ``co:`` class name for an alias: hyphen/underscore
    segments capitalised (``source-person`` → ``SourcePerson``) — the shape
    the shipped VOCABULARY.md uses for its built-in classes."""
    parts = [p for p in re.split(r"[-_]+", alias) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or alias


def _existing_class_names(text: str) -> "set[str]":
    """Every ``co:`` name already declared by a real heading line (fenced
    blocks excluded by the same skip the type parser uses)."""
    names: "set[str]" = set()
    fence: Optional[str] = None
    for line in text.splitlines():
        fence_m = _FENCE_RE.match(line)
        if fence_m:
            delim = fence_m.group("delim")
            if fence is None:
                fence = delim
            elif delim == fence:
                fence = None
            continue
        if fence is not None:
            continue
        m = _CLASS_HEADING_RE.match(line)
        if m:
            names.add(str(m.group("name")))
    return names


def extend_vocabulary(
    project_root: Union[str, Path],
    new_types: Iterable[object],
) -> VocabularyExtension:
    """APPEND undeclared, wellformed node types to the project's
    ``knowledge/VOCABULARY.md`` — the owner-directed auto-extend (P299:
    "auto-extend the vocabulary instead of rejecting").

    Strictly append-only: the file is user data, so no existing byte is
    ever rewritten — new declarations go to the END of the file, under an
    :data:`AUTO_SECTION_MARKER` section that is created on the first
    extension. A missing file is created with a minimal header (never a
    copy of the shipped ontology — that is the installer's job, and
    clobbering a user's absent-by-choice file with 464 shipped lines is
    not this function's call).

    Each declaration is a real class heading in the canonical shape the
    parser requires (``#### **`co:SourcePerson`** (alias: `source-person`)``)
    so the very next :func:`load_vocabulary` call sees the type. The alias
    is the LOWERCASED input (the parser lowercases anyway); no Folder line
    is written, so the type files under ``knowledge/concepts/`` — the same
    default the built-in pattern/insight/guide types use.

    Never raises: any OSError/decode problem is returned as
    ``VocabularyExtension.error`` (soft-fail — the caller reports loudly
    and carries on; a vocabulary write failure must never drop a node,
    which is the very defect this mechanism closes). Invalid inputs are
    recorded in ``invalid`` and never appended. Idempotent: an already
    declared type (this file's fresh parse wins, case-insensitive) is a
    no-op.
    """
    vocab_path = Path(project_root) / "knowledge" / "VOCABULARY.md"
    # N3 (Opus branch review 2026-10-06): the parse-classify-append below
    # is a read-modify-write over user data that now runs CONCURRENTLY
    # (detached install seeds beside live store_knowledge_node calls and
    # per-edit syncs). Two unlocked writers can both append the AUTO
    # section header or duplicate ``co:`` headings, and on a missing file
    # one ``write_text`` can clobber the other's new file. Serialize on
    # the shared lock home (vco_lib.atomic.exclusive_file_lock — the same
    # sidecar-flock idiom the router's counters use). Bounded wait: a
    # timeout soft-fails exactly like an OSError below (the caller
    # reports loudly and carries on; the next sync re-declares —
    # self-healing by design, same as the pre-lock failure modes).
    try:
        with exclusive_file_lock(
            vocab_path.parent / ".vocabulary.lock", timeout_s=5.0
        ):
            return _extend_vocabulary_locked(vocab_path, project_root, new_types)
    except (OSError, LockTimeout) as exc:
        return VocabularyExtension(
            vocabulary_path=str(vocab_path),
            error=f"{exc.__class__.__name__}: {exc}",
        )


def _extend_vocabulary_locked(
    vocab_path: Path,
    project_root: Union[str, Path],
    new_types: Iterable[object],
) -> VocabularyExtension:
    """The parse-classify-append body of :func:`extend_vocabulary`.

    Caller holds the vocabulary lock (or is a single-process test); never
    raises — same soft-fail contract as the public wrapper.
    """
    # Fresh parse (no cache) so a same-process second call sees the first
    # call's append even on a coarse-mtime filesystem.
    vocab = load_vocabulary(project_root, use_cache=False)

    added_order: "list[str]" = []   # lowercased aliases, first-seen order
    already: "list[str]" = []
    invalid: "list[str]" = []
    seen: "set[str]" = set()
    for raw in new_types:
        if not is_wellformed_type(raw):
            token = raw.strip() if isinstance(raw, str) else repr(raw)
            if token not in invalid:
                invalid.append(token)
            continue
        alias = str(raw).strip().lower()
        if alias in vocab.node_types:
            if alias not in already:
                already.append(alias)
            continue
        if alias in seen:
            continue
        seen.add(alias)
        added_order.append(alias)

    if not added_order:
        return VocabularyExtension(
            already_declared=tuple(already),
            invalid=tuple(invalid),
            vocabulary_path=str(vocab_path),
        )

    try:
        existed = vocab_path.is_file()
        current_text = ""
        if existed:
            # errors="replace": a mis-encoded file still gets a valid
            # append (bytes preserved — we never rewrite what is there).
            current_text = vocab_path.read_text(
                encoding="utf-8", errors="replace"
            )
        used_names = _existing_class_names(current_text)

        blocks: "list[str]" = []
        if not existed:
            blocks.append(_NEW_FILE_HEADER)
        if AUTO_SECTION_MARKER not in current_text:
            blocks.append(
                f"{AUTO_SECTION_MARKER}\n{_AUTO_SECTION_HEADING}\n"
            )
        for alias in added_order:
            name = _pascal_class_name(alias)
            if name in used_names:
                name = alias          # valid charset; keeps the heading unique
            if name in used_names:
                # GLM wave-2 review nit 5: the raw alias can ALSO be a taken
                # class name (a file declaring `co:source-person` under a
                # different alias). Duplicate `co:` headings are cosmetic
                # (the parser is alias-keyed) but sloppy — take the smallest
                # free numeric suffix instead.
                suffix = 2
                while f"{alias}-{suffix}" in used_names:
                    suffix += 1
                name = f"{alias}-{suffix}"
            used_names.add(name)
            blocks.append(
                f"#### **`co:{name}`** (alias: `{alias}`)\n"
                f"- **Definition**: Auto-declared by VCO — a node used this "
                f"undeclared type and the vocabulary is open. Replace this "
                f"line with a real definition; keep the heading shape.\n\n"
            )
        block = "".join(blocks)

        vocab_path.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            # Byte-preserving append: open in append mode and never touch
            # the existing bytes. The leading "\n" terminates a last line
            # that lacks its newline AND separates the block visually when
            # the file already ends with one.
            with open(vocab_path, "a", encoding="utf-8") as fh:
                fh.write("\n" + block)
        else:
            vocab_path.write_text(block, encoding="utf-8")
    except OSError as exc:
        return VocabularyExtension(
            already_declared=tuple(already),
            invalid=tuple(invalid),
            vocabulary_path=str(vocab_path),
            error=f"{exc.__class__.__name__}: {exc}",
        )

    # Refresh this process's cache entry so a long-lived consumer (the MCP
    # server) sees the declaration immediately, without an mtime race.
    load_vocabulary(project_root, use_cache=False)

    return VocabularyExtension(
        added=tuple(added_order),
        already_declared=tuple(already),
        invalid=tuple(invalid),
        vocabulary_path=str(vocab_path),
    )
