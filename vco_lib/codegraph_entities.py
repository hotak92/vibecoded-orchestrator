# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""``CodeEntity`` — the intermediate representation the code-graph analyzer's
extractors emit, and the ONE place that maps an entity to the
``insert_params`` dict + identity key the ``_dedup_insert`` choke-point
consumes (P2f stage 1, v0.2.76).

WHY THIS EXISTS
---------------
Before P2f the 14 ``_analyze_*_file`` extractors in
``templates/scripts/analyze_code_graph.py`` each hand-built a
``{"properties": {...}, "references": {...}, "_deferred_embed": ...}`` dict and
repeated the identity-key expression
``insert_params["properties"].get("full_name", insert_params["properties"]["name"])``
at ~20 near-identical call-sites (and the ``endpoint + ":" + method`` variant at
the CodeAPI sites). That per-site duplication is what P2f collapses: each
extractor now constructs a typed ``CodeEntity`` and calls one
``analyzer.store_entity(entity)`` wrapper, which delegates to
``entity.to_insert_params()`` + ``entity.identity_key()`` and then the UNCHANGED
``_dedup_insert`` write path (dedup / fingerprint / chunking mechanics stay
single-homed where v0.2.72 already put them — this IR does NOT move them).

BYTE-IDENTICAL CONTRACT
-----------------------
``to_insert_params()`` must reproduce EXACTLY the dict each site built before —
same property keys, same values, same ``references`` shape, same
``_deferred_embed``/``vector`` wiring. The golden snapshots
(``tests/test_codegraph_golden.py``) lock this: any change to the emitted
property set is a semantic regression. Because extractors differ in WHICH
optional properties they emit (python classes carry ``field_types`` +
``composes``; the regex extractors do not; only python functions carry
``type_uses``; CodeAPI carries ``endpoint``/``method``/``parameters``/...),
every optional field here is emitted ONLY when explicitly provided. Anything
that does not fit a named field goes through ``extras`` (see the per-kind list
below) so no divergence is silently unioned in.

PER-KIND ``extras`` INVENTORY (enumerated from the pre-P2f call-sites)
---------------------------------------------------------------------
* CLASS:
    - ``methods`` (list[str])          — regex extractors + python
    - ``field_types`` (list[str])      — PYTHON ONLY
    - ``composes`` (list[str])         — PYTHON ONLY
* FUNCTION:
    - ``type_uses`` (list[str])        — PYTHON ONLY
* API (CodeAPI — a wholly distinct property set, all via extras):
    - ``endpoint``, ``method``, ``api_description``, ``parameters``,
      ``returns``, ``proxy_target``
* INTERACTION (CodeInteraction — distinct property set + a distinct
  reference shape ``source_module`` / ``source_function``, all via extras):
    - ``source_project``, ``interaction_type``, ``direction``, ``protocol``,
      ``endpoint``, ``raw_target``, ``confidence``, ``description``
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

# Entity kinds. These map to the analyzer's five collections; the analyzer's
# ``store_entity`` wrapper picks the collection from ``kind``.
KIND_MODULE = "module"
KIND_CLASS = "class"
KIND_FUNCTION = "function"
KIND_API = "api"
KIND_INTERACTION = "interaction"


@dataclass
class CodeEntity:
    """One extracted code entity, language-agnostic.

    Only ``kind`` and ``file_path_rel`` are always meaningful. ``name`` /
    ``full_name`` drive the dedup identity for module/class/function; API and
    interaction entities key off ``extras`` (endpoint+method / source+endpoint)
    and put their whole property set in ``extras``.

    Set a named field to include it in the emitted properties; leave it ``None``
    to OMIT it (byte-identical contract — do not emit a key a site never set).
    ``extras`` values are merged verbatim into the properties dict. ``project``
    defaults ``None`` here so a site that does not stamp ``project`` (rare) is
    faithful; every real emit site passes it.
    """

    kind: str
    file_path_rel: str = ""

    # Common named properties (each emitted only when not None).
    name: Optional[str] = None
    full_name: Optional[str] = None
    signature: Optional[str] = None
    doc: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    is_async: Optional[bool] = None
    project: Optional[str] = None

    # Body carries under the collection-appropriate key: class → ``class_body``,
    # function → ``function_body``. Emitted only when not None.
    body: Optional[str] = None

    # Per-kind oddities that do not warrant a named field (see module docstring
    # inventory). Merged verbatim into properties.
    extras: Dict[str, Any] = field(default_factory=dict)

    # Cross-reference UUIDs. For most kinds ``{"module": <uuid>}``; interaction
    # uses ``{"source_module": ..., "source_function": ...}``. Merged verbatim.
    references: Dict[str, str] = field(default_factory=dict)

    # Embedding wiring — exactly one of these is typically set:
    #   * ``deferred_embed``: a zero-arg callable resolved lazily by
    #     ``_dedup_insert`` (the FIX-B2 embed-skip path). Emitted as
    #     ``insert_params["_deferred_embed"]``.
    #   * ``vector``: an already-shaped eager vector (API / interaction /
    #     module sites embed eagerly). Emitted as ``insert_params["vector"]``
    #     ONLY when truthy (matching the ``if embedding:`` guards).
    deferred_embed: Optional[Callable[[], Any]] = None
    vector: Optional[Any] = None

    # ---- Body-property key by kind -------------------------------------------

    def _body_key(self) -> str:
        return "class_body" if self.kind == KIND_CLASS else "function_body"

    # ---- Identity key (the ~20×-duplicated expression, now single-homed) -----

    def identity_key(self) -> str:
        """The dedup identity string ``_dedup_insert`` keys the deterministic
        UUID on. Mirrors the pre-P2f per-site expressions exactly:

          * module/class/function → ``full_name`` if set else ``name``;
          * api → ``endpoint + ":" + method``;
          * interaction → ``ix::<source>::<endpoint>`` (built by the caller and
            passed as ``extras['_identity_key']`` because the source token is
            not otherwise a property).
        """
        if self.kind == KIND_API:
            props = self.extras
            return f"{props.get('endpoint', '')}:{props.get('method', '')}"
        if self.kind == KIND_INTERACTION:
            # The interaction identity uses a source token that is not stored as
            # a property; the caller supplies the exact key via extras.
            return str(self.extras.get("_identity_key", ""))
        return self.full_name or self.name or ""

    # ---- insert_params assembly (byte-identical to the pre-P2f dicts) --------

    def to_insert_params(self) -> Dict[str, Any]:
        """Build the ``insert_params`` dict for ``_dedup_insert``.

        Emits ONLY the named fields that were set, plus ``extras`` verbatim
        (minus the private ``_identity_key`` control key), ``references`` when
        non-empty, and exactly one embedding key when provided.
        """
        props: Dict[str, Any] = {}

        # Named fields, in a stable order, each only when provided. Order here
        # does not affect Weaviate or the content hash (which selects fixed
        # fields), and the golden snapshot sorts keys — but keep it readable.
        if self.name is not None:
            props["name"] = self.name
        if self.full_name is not None:
            props["full_name"] = self.full_name
        if self.body is not None:
            props[self._body_key()] = self.body
        if self.signature is not None:
            props["signature"] = self.signature
        if self.doc is not None:
            props["doc"] = self.doc
        if self.start_line is not None:
            props["start_line"] = self.start_line
        if self.end_line is not None:
            props["end_line"] = self.end_line
        if self.is_async is not None:
            props["is_async"] = self.is_async
        if self.project is not None:
            props["project"] = self.project

        # Merge extras verbatim (per-kind oddities + full API/interaction sets),
        # skipping the private identity-control key.
        for k, v in self.extras.items():
            if k == "_identity_key":
                continue
            props[k] = v

        insert_params: Dict[str, Any] = {"properties": props}
        if self.references:
            insert_params["references"] = dict(self.references)
        if self.deferred_embed is not None:
            insert_params["_deferred_embed"] = self.deferred_embed
        if self.vector:
            insert_params["vector"] = self.vector
        return insert_params
