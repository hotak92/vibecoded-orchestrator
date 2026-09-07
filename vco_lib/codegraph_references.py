# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home for code-graph cross-reference EDGES: resolve, read, write.

Three surfaces read a Weaviate cross-reference and every one of them was
broken the same way, so the normalisation lives here and nowhere else:

  * ``claude_mcp_servers/weaviate_mcp/server.py::query_code_structure`` —
    the ``dependencies`` and ``extends`` branches (MCP).
  * ``templates/scripts/query_code_graph.py::query_structure`` — the
    ``dependencies`` and ``extends`` branches (the user-facing
    ``.claude/scripts/code-graph-query structure`` CLI).
  * ``templates/scripts/analyze_code_graph.py::create_cross_references`` —
    the WRITE pass, which has to read the CURRENT beacon set before it can
    decide whether an edge already exists.

Why a shared home (CLAUDE.md § "search before you add, extract before you
duplicate"): the CLI and the MCP had independently open-coded
``refs.get(name, [])`` + ``len()`` + ``for``. Adding a third inline copy for
the analyzer would have made four call sites of one concern.

The two failure modes this module exists to absorb
--------------------------------------------------
They are DIFFERENT problems and were fixed at different times, which is
exactly why the second one survived the first fix:

1. **``obj.references`` is ``None``** when a fetch resolves no links.
   ``obj.references.get(...)`` then raises ``AttributeError``. Guarded in the
   CLI by v0.2.70 C1c (``... or {}``), and in the MCP by v0.2.92.

2. **``obj.references[name]`` is a ``_CrossReference``, not a list.** Verified
   against weaviate-client 4.21.0::

       len(cross)        -> TypeError: object of type '_CrossReference' has no len()
       iter(cross)       -> TypeError: '_CrossReference' object is not iterable
       cross[0]          -> TypeError: '_CrossReference' object is not subscriptable
       bool(cross)       -> True, ALWAYS (even with zero objects)
       cross.objects     -> [...]   # the resolved targets actually live here

   So the None-guard alone leaves the SUCCESS path broken: the moment
   references genuinely resolve, ``len()``/``for`` raise. The CLI carried
   exactly that state between v0.2.70 and v0.2.92.

   The ``bool(cross) is True`` line above is the trap that makes a
   hand-rolled ``if cross:`` guard look correct while doing nothing.

A plain ``list`` is ALSO accepted, deliberately: older clients returned one,
and every test fake in this repo supplies one. A list-only implementation is
what let defect (2) live in shipped code — the tests were green against
list-shaped fakes while production returned ``_CrossReference``. Any test of
this module must therefore exercise the REAL
``weaviate.collections.classes.internal._CrossReference``; see
``tests/test_v0292_codegraph_reference_normalise.py``.

Beyond the read path this module also owns the analyzer's pure edge
RESOLUTION (name → target id) and the add-if-absent WRITE — see the section
banners below. Those moved out of ``analyze_code_graph.py`` in v0.2.92: the
analyzer is a 7.3k-line bundled template script where the same logic could not
be unit-tested, and the repo's ratchet on that file is downward-only.

This module is intentionally weaviate-free (pure duck-typing) so it imports
in any environment and its tests need no client.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "normalize_reference_targets",
    "read_cross_reference",
    "reference_target_uuids",
    "dedup_ref_targets",
    "add_missing_reference_edges",
    "build_short_name_index",
    "build_module_name_index",
    "resolve_base_class_targets",
    "resolve_import_target_path",
    "NON_PROJECT_BASE_CLASSES",
]


# ── Edge RESOLUTION (moved out of analyze_code_graph.py, v0.2.92) ───────────
# `create_cross_references` decides which target each discovered name points at
# before it can write anything. That decision is pure — names in, target ids
# out — so it belongs beside the read/write plumbing rather than inline in a
# 7.3k-line bundled template script where it cannot be unit-tested. The
# analyzer keeps only the loops that own the caches and the collections.
#
# Bases the analyzer deliberately does NOT link: builtins, stdlib and popular
# third-party roots that are not project classes. Kept as a frozenset for
# membership (the analyzer had it inline as a tuple literal); order is
# irrelevant, the contents are the contract.
NON_PROJECT_BASE_CLASSES = frozenset({
    "object", "Exception", "BaseException",
    "ABC", "Protocol", "TypedDict", "Enum",
    "IntEnum", "StrEnum", "BaseModel",
    "unittest.TestCase", "TestCase",
    "str", "int", "float", "bytes", "dict",
    "list", "tuple", "set", "frozenset",
    "type", "Generic", "NamedTuple",
    "Thread", "Process", "Handler",
    "logging.Handler",
})


def build_short_name_index(full_names: Iterable[str]) -> Dict[str, List[str]]:
    """``short name -> [full names]``, keyed on the last dotted segment.

    Used for both functions and classes: a call/base written as ``helper`` has
    to find ``pkg.mod.helper``.
    """
    index: Dict[str, List[str]] = {}
    for full_name in full_names:
        index.setdefault(full_name.rsplit(".", 1)[-1], []).append(full_name)
    return index


def build_module_name_index(paths: Iterable[str]) -> Dict[str, List[str]]:
    """``import name -> [module paths]``.

    Each path is indexed twice: by stem (``src/foo/bar.py`` → ``bar``) and by
    its last two dotted segments (→ ``foo.bar``), so both ``import bar`` and
    ``import foo.bar`` resolve.
    """
    index: Dict[str, List[str]] = {}
    for path in paths:
        index.setdefault(Path(path).stem, []).append(path)
        parts = Path(path).with_suffix("").parts
        if len(parts) > 1:
            index.setdefault(".".join(parts[-2:]), []).append(path)
    return index


def resolve_base_class_targets(
    signature: str,
    class_cache: Mapping[str, str],
    class_name_to_full: Mapping[str, List[str]],
) -> List[str]:
    """Target UUIDs for the project-local base classes named in ``signature``.

    ``signature`` is the stored ``class Foo(Bar, Baz)`` line. Bases in
    :data:`NON_PROJECT_BASE_CLASSES` and bases with no matching project class
    are skipped; a signature with no parenthesised base list yields ``[]``.
    Duplicates are NOT collapsed here — :func:`add_missing_reference_edges`
    owns that, so this stays a faithful "what does the signature name?".
    """
    match = re.search(r"\(([^)]+)\)", signature or "")
    if not match:
        return []
    out: List[str] = []
    for base_name in (b.strip() for b in match.group(1).split(",")):
        if base_name in NON_PROJECT_BASE_CLASSES:
            continue
        if base_name in class_cache:
            out.append(class_cache[base_name])
            continue
        candidates = class_name_to_full.get(base_name, [])
        if not candidates:
            continue
        out.append(class_cache[candidates[0]])
    return out


def resolve_import_target_path(
    imp_name: str, module_name_to_path: Mapping[str, List[str]]
) -> Optional[str]:
    """Module path an import name points at, or ``None``.

    Tries the name as written, then its last dotted component (``foo.bar`` →
    ``bar``). First candidate wins — cross-ref linking is best-effort and a
    name shared by two modules resolves to A valid row.
    """
    candidates = module_name_to_path.get(imp_name, [])
    if not candidates:
        candidates = module_name_to_path.get(imp_name.rsplit(".", 1)[-1], [])
    return candidates[0] if candidates else None


def normalize_reference_targets(value: Any) -> List[Any]:
    """Plain list of target objects for ONE already-looked-up reference value.

    ``value`` is whatever ``obj.references.get(link_on)`` returned:

    * ``None`` (link absent, or the whole ``references`` mapping was None)
      → ``[]``
    * a ``_CrossReference`` wrapper → its ``.objects``
    * a plain list/tuple (older clients, test fakes) → a copy of it

    Never raises: an unrecognised shape yields ``[]``. Callers render user
    output from this, and a read-path shape surprise must degrade to "no
    links" rather than crash the query (the conservative read-side contract
    shared with ``codegraph_vector_copy``).
    """
    if value is None:
        return []
    # _CrossReference exposes the resolved rows on `.objects`. Check this
    # BEFORE the sequence check: the wrapper is truthy and non-iterable, so
    # any list-shaped test would silently fall through to the [] return.
    objects = getattr(value, "objects", None)
    if objects is not None:
        return list(objects)
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def read_cross_reference(obj: Any, link_on: str) -> List[Any]:
    """Resolved target objects for ``obj``'s ``link_on`` cross-reference.

    Fuses both guards described in the module docstring: the ``references is
    None`` guard and the ``_CrossReference`` shape normalisation. Never
    raises; ``[]`` covers "no references resolved", "this link was not
    requested" and "unexpected shape" alike.
    """
    refs = getattr(obj, "references", None)
    if not refs:
        return []
    try:
        value = refs.get(link_on)
    except AttributeError:  # not a mapping — nothing we can read
        return []
    return normalize_reference_targets(value)


def reference_target_uuids(obj: Any, link_on: str) -> List[str]:
    """The stored beacon target UUIDs (as ``str``) for ``obj``'s ``link_on``.

    The WRITE side's question. ``create_cross_references`` needs to know which
    edges an object ALREADY carries so it can add only the missing ones —
    ``data.reference_add`` is not idempotent, and re-adding a stored edge
    creates a second beacon for it (live data on the maintainer machine holds
    66 identical beacons for a single edge, and 1518 ``calls`` beacons on
    one module).

    Duplicates are preserved here on purpose: this is a faithful read of what
    is stored, and the caller compares membership, not length. Rows whose
    ``uuid`` is missing are skipped rather than turned into ``"None"``.
    """
    out: List[str] = []
    for target in read_cross_reference(obj, link_on):
        uid = getattr(target, "uuid", None)
        if uid is None:
            continue
        out.append(str(uid))
    return out


def dedup_ref_targets(
    objects: Iterable[Any], prop_names: Sequence[str] | Tuple[str, ...]
) -> List[Any]:
    """One row per distinct identity, first-seen order preserved.

    The read-side companion of :func:`reference_target_uuids`: while the
    analyzer fix stops NEW duplicate beacons from being created, the beacons
    already stored are not retro-actively removed (collapsing them is a
    separate, consent-gated data operation). Without this, ``dependencies``
    answers "what does X import?" with 66 copies of one path.

    Identity is the first present property in ``prop_names``. A row carrying
    none of them is kept as-is — never merged with another anonymous row,
    since we have no evidence they are the same target.
    """
    seen: set = set()
    out: List[Any] = []
    for obj in objects:
        try:
            props = obj.properties or {}
        except Exception:  # noqa: BLE001 — defensive on mocked/partial objects
            props = {}
        key = None
        for name in prop_names:
            value = props.get(name)
            if value:
                key = (name, value)
                break
        if key is None:
            out.append(obj)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(obj)
    return out


def add_missing_reference_edges(
    collection: Any,
    from_uuid: str,
    from_property: str,
    target_uuids: Iterable[Any],
    existing: Iterable[Any] = (),
) -> int:
    """Create only the ``from_uuid -> target`` edges that are not stored yet.

    Returns the number of edges actually created. Raises whatever the client
    raises — callers keep their own soft-fail wrapper so one bad edge does not
    abort a whole analyze pass.

    Why add-if-absent rather than ``reference_replace``
    ---------------------------------------------------
    ``data.reference_add`` is NOT idempotent: Weaviate stores each call as its
    own beacon, so re-running the analyzer over an unchanged file multiplied
    that file's stored edges on every pass — unbounded growth, and every
    consumer then sees N copies of one edge (live data on the maintainer
    machine: 1518 ``calls`` beacons on a single CodeFunction row, 506 ``extends``
    beacons for a single base class, 5 for one `rl_archive -> paths` edge).

    ``reference_replace(to=[...])`` is the tidier primitive — authoritative,
    it also drops edges whose source line was deleted, and it would collapse
    the beacons already stored. It is deliberately NOT used here:

    * The analyzer's desired set is derived from caches filled by
      ``_populate_caches_from_weaviate``, whose whole-collection scan
      SOFT-FAILS (``except Exception: print(warning)``). A partial scan yields
      a partial desired set, and ``replace`` would then DELETE real edges it
      merely failed to see. Per CLAUDE.md's "conservative defaults on
      best-effort paths", an operation that cannot positively confirm its
      precondition must do nothing rather than guess. Add-if-absent cannot
      lose an edge; replace can.
    * Collapsing beacons ALREADY stored mutates live collections, i.e. a
      separate consent-gated data operation — not a side effect an ordinary
      incremental re-analysis should perform.

    Consequences, stated plainly: this stops the growth and makes re-analysis
    idempotent, but it does NOT shrink beacon sets that are already
    duplicated (the read side collapses those for display) and it does NOT
    remove an edge whose source line was deleted. Both are pre-existing gaps
    that a ``reference_replace`` upgrade would close once the cache scan
    fail-loudly guarantees a complete desired set.

    ``existing`` is compared as strings, so callers may pass raw client
    ``uuid`` objects or the ``str`` values from :func:`reference_target_uuids`.
    """
    already = {str(u) for u in existing}
    added = 0
    for target in target_uuids:
        key = str(target)
        if key in already:
            continue
        collection.data.reference_add(
            from_uuid=from_uuid,
            from_property=from_property,
            to=key,
        )
        # Guards the INTRA-pass repeat too: two different import statements can
        # resolve to the same module row, and the pre-fix loop wrote a beacon
        # for each one.
        already.add(key)
        added += 1
    return added
