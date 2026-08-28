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
* ``templates/scripts/sync_knowledge_graph.py`` — the node validator
  delegates here (A-leg); its inline ImportError-fallback parser MUST match
  ``parse_vocabulary_text``'s type extraction (same parity test).

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
from typing import Mapping, Optional, Union

__all__ = [
    "BUILTIN_NODE_TYPES",
    "BUILTIN_KNOWLEDGE_SUBFOLDERS",
    "BUILTIN_NODE_TYPE_TO_FOLDER",
    "DEFAULT_NODE_FOLDER",
    "RL_TYPE_CAPACITY_SOFT_CAP",
    "KgVocabulary",
    "builtin_vocabulary",
    "parse_vocabulary_text",
    "load_vocabulary",
    "clear_vocabulary_cache",
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
