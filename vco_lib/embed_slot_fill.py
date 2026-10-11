# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Named-vector slot fill — the store-agnostic core shared by the code graph
(:mod:`vco_lib.code_embed_model`) and the knowledge graph
(:mod:`vco_lib.kg_embed_model`).

v0.2.102 W6-I. Both stores follow one design (owner ruling 2026-10-10): a
RECORDED model whose slot is complete serves searches, the selected TARGET is
filled additively into its own named vector, and the record flips once the
target's slot is complete. What that design needs from Weaviate is the same for
both stores, so it lives here once:

* :func:`declared_slots` — which named vectors a class declares (REST schema);
* :func:`sample_slot_population` — per slot, how many of the sampled objects
  hold it (``{slot: (dim, count)}`` + the sample size) — a cheap live read,
  never a completeness proof;
* :func:`sample_common_vectors` — the slots populated on EVERY sampled object
  (the every-object filter over :func:`sample_slot_population`);
* :func:`object_count` — the GraphQL aggregate count;
* :func:`scan_slot_counts` — the objective completeness measure: a cursor scan
  counting, per slot, the objects that hold a vector there;
* :func:`ensure_slot` — check or make (additively) a set of classes declare a
  slot (:func:`vco_lib.weaviate_schema.add_named_vector_slot`, vector-preserving);
* :func:`same_model` — "do two model ids embed into the same vector space?".

Every reader soft-fails to "no verdict" (``None`` / ``{}`` / ``0``) — never a
guess. Callers decide what an absent verdict means.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Optional


def declared_slots(name: str, weaviate_url: Optional[str]) -> Optional[set]:
    """Named-vector slots ``name`` declares; ``None`` when the class does not
    exist (or the schema cannot be read)."""
    from vco_lib import weaviate_helpers as _wh

    schema = _wh.fetch_schema(name, weaviate_url=weaviate_url)
    if schema is None:
        return None
    return set((schema.get("vectorConfig") or {}).keys())


def sample_slot_population(
    name: str, weaviate_url: Optional[str], limit: int,
) -> "tuple[dict[str, tuple[int, int]], int]":
    """``({slot: (dim, objects_holding_it)}, sampled)`` for up to ``limit``
    stored objects — the per-slot populations of ONE sample, in first-seen
    order. ``({}, 0)`` on an empty class or any failure (no verdict, never a
    guess).

    This is the raw read; the two consumers differ in what they conclude:
    :func:`sample_common_vectors` keeps only the slots EVERY sampled object
    holds, while the KG's live record read
    (:func:`vco_lib.kg_embed_model.record_from_live`) follows a MAJORITY slot,
    so a handful of objects rewritten target-only cannot empty the answer
    (W6-J, review F1)."""
    from vco_lib import weaviate_helpers as _wh

    base = (weaviate_url or _wh.weaviate_url_default()).rstrip("/")
    status, body = _wh.http_request(
        "GET", f"{base}/v1/objects?class={name}&limit={int(limit)}&include=vector",
        timeout=10.0,
    )
    if status != 200:
        return {}, 0
    objs = json.loads(body.decode("utf-8")).get("objects") or []
    population: "dict[str, tuple[int, int]]" = {}
    for obj in objs:
        vectors = obj.get("vectors") or {}
        for slot, vec in vectors.items():
            if isinstance(vec, list) and vec:
                dim, count = population.get(slot, (len(vec), 0))
                population[slot] = (dim, count + 1)
    return population, len(objs)


def sample_common_vectors(name: str, weaviate_url: Optional[str], limit: int) -> "dict[str, int]":
    """``{slot: dim}`` for the named vectors populated on EVERY sampled stored
    object (up to ``limit``) — a slot some sampled object lacks is not a
    complete slot. ``{}`` on an empty class or any failure."""
    population, sampled = sample_slot_population(name, weaviate_url, limit)
    if sampled <= 0:
        return {}
    return {slot: dim for slot, (dim, count) in population.items() if count == sampled}


def object_count(name: str, weaviate_url: Optional[str]) -> int:
    """Objects in ``name`` (GraphQL aggregate); 0 when it cannot be asked."""
    from vco_lib import weaviate_helpers as _wh

    base = (weaviate_url or _wh.weaviate_url_default()).rstrip("/")
    status, body = _wh.http_request(
        "POST", f"{base}/v1/graphql",
        body={"query": f"{{ Aggregate {{ {name} {{ meta {{ count }} }} }} }}"},
        timeout=10.0,
    )
    if status != 200:
        return 0
    data = json.loads(body.decode("utf-8"))
    return int(data["data"]["Aggregate"][name][0]["meta"]["count"])


def _default_client():
    from vco_lib import weaviate_helpers as _wh

    return _wh.connect_v4()


def scan_slot_counts(
    collections: Iterable[str], slots: Iterable[str], *,
    client_factory: Optional[Callable[[], Any]] = None,
) -> "Optional[tuple[dict[str, int], int]]":
    """``({slot: filled}, total)`` across ``collections`` (missing classes are
    skipped): per slot, the objects holding a vector there, vs all objects.

    Weaviate cannot filter on "vector missing", so this is a cursor scan with
    vectors (the client's iterator takes no per-slot selection). ``None`` when
    Weaviate cannot be asked — never a guess."""
    wanted = list(dict.fromkeys(slots))
    client = None
    try:
        client = (client_factory or _default_client)()
        counts = {s: 0 for s in wanted}
        total = 0
        for name in collections:
            if not client.collections.exists(name):
                continue
            coll = client.collections.get(name)
            for obj in coll.iterator(include_vector=True, return_properties=[]):
                total += 1
                vec = obj.vector
                if not isinstance(vec, dict):
                    continue
                for slot in wanted:
                    if vec.get(slot):
                        counts[slot] += 1
        return counts, total
    except Exception:  # noqa: BLE001 — unreachable → no verdict
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def ensure_slot(
    collections: Iterable[str], slot: str, dim: int, catalog: Iterable[Any], *,
    model: str, add: bool, weaviate_url: Optional[str] = None,
    slot_adder: Optional[Callable[..., Any]] = None,
    declared: Optional[Callable[[str, Optional[str]], Optional[set]]] = None,
) -> Optional[str]:
    """Check (``add=False``) or make (``add=True``) every EXISTING class in
    ``collections`` declare ``slot``. Returns an error message, or None.

    The add is the additive, vector-preserving slot-add
    (:func:`vco_lib.weaviate_schema.add_named_vector_slot`); it never drops
    data. An unreachable Weaviate gives no verdict (the caller's own connect
    reports it); a class that does not exist yet needs no slot."""
    from vco_lib.weaviate_schema import AddSlotResult, NamedVectorSlot

    by_name = {s.name: s for s in catalog}
    if slot in by_name and by_name[slot].dim != dim:
        return (f"{model} is {dim}-dimensional but the slot it maps to "
                f"({slot}) is declared at {by_name[slot].dim}; pick a model whose "
                "dimension matches, or a different backend.")
    if slot_adder is None:
        from vco_lib.weaviate_schema import add_named_vector_slot as slot_adder
    read_declared = declared or declared_slots
    for name in collections:
        try:
            have = read_declared(name, weaviate_url)
        except Exception:  # noqa: BLE001
            return None
        if have is None or slot in have:
            continue
        if not add:
            return (f"{name} does not declare the {slot} named vector that {model} "
                    "embeds into — its stored vectors are from another model.")
        try:
            result = slot_adder(name, by_name.get(slot) or NamedVectorSlot(slot, dim),
                                weaviate_url=weaviate_url)
        except Exception as exc:  # noqa: BLE001
            return f"adding the {slot} named vector to {name} failed: {exc}"
        if result == AddSlotResult.DimMismatchError:
            return f"{name} already stores {slot} vectors of another dimension."
    return None


def same_model(
    a: str, b: str, slot_of: Callable[[str], "tuple[str, int]"],
    norm: Callable[[str], str], shared_slots: "Iterable[str]",
) -> bool:
    """True when two model ids embed into the same vector space: same slot and
    dim, and — in a slot several models share — the same model name."""
    sa, da = slot_of(a)
    sb, db = slot_of(b)
    if (sa, da) != (sb, db):
        return False
    return sa not in set(shared_slots) or norm(a) == norm(b)


def counters_complete(counts: Mapping[str, int], total: int, slot: str) -> bool:
    """A slot is COMPLETE when every one of a non-empty set of objects holds
    it. An empty set proves nothing (no flip onto a slot no object proves)."""
    return total > 0 and int(counts.get(slot, 0)) == total


def reconcile_folder_embeddings(folder: Any, *, dry_run: bool = False) -> None:
    """The bundle run's embedding-model self-heal, for BOTH stores (every
    ``install-bundle`` and the root bundle inside install.py): the code graph
    (:func:`vco_lib.code_embed_model.reconcile_folder_code_embedding`) and the
    KG collections (:func:`vco_lib.kg_embed_model.reconcile_folder_kg_embedding`).
    No embedding happens here; each half soft-fails on its own."""
    from vco_lib.code_embed_model import reconcile_folder_code_embedding
    from vco_lib.kg_embed_model import reconcile_folder_kg_embedding

    reconcile_folder_code_embedding(folder, dry_run=dry_run)
    reconcile_folder_kg_embedding(folder, dry_run=dry_run)
