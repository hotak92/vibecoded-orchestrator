# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Give an existing Weaviate class the named vectors it lacks — the ONE home
for the manual migration tools (v0.2.107, DESTRUCTIVE S-2).

Callers: ``claude_mcp_servers/scripts/migrate_to_new_embeddings.py``
(``add_named_vector_to_collection``) and the ``migrate_embeddings`` function
of ``claude_mcp_servers/weaviate_mcp/server.py``.

Two cases, decided by the live schema:

* The class already declares named vectors (``vectorConfig``): the missing
  slots are added IN PLACE through
  :func:`vco_lib.weaviate_schema.add_vector_slots_in_place`. Nothing is read,
  dropped or re-inserted. A server that cannot add a slot in place refuses
  with the message that says to update Weaviate.
* A legacy single-vector class (no ``vectorConfig``) cannot take a named
  vector beside its unnamed one, so it is rebuilt — and only after
  :func:`snapshot_rows` proved the listing whole: the rows read must equal
  :func:`vco_lib.weaviate_helpers.http_count_objects`. An unreadable count
  or a mismatch raises :class:`MigrationRefused` before anything is dropped.
  The rebuild re-inserts every row under its ORIGINAL uuid.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from vco_lib import weaviate_helpers as _wh
from vco_lib import weaviate_schema as _ws
from vco_lib.codegraph_cursor import iter_class

__all__ = [
    "MigrationRefused",
    "add_missing_slots_in_place",
    "rebuild_with_named_vectors",
    "snapshot_rows",
]


class MigrationRefused(RuntimeError):
    """The migration did not run; the class is exactly as it was."""


def add_missing_slots_in_place(
    name: str, slots: Mapping[str, int], *, weaviate_url: Optional[str] = None,
) -> Optional[List[str]]:
    """Add the slots ``{slot name: dim}`` the class lacks, in place.

    Returns the names added (``[]`` when every slot is already there), or
    ``None`` when the class is a legacy single-vector class — the one case
    that needs :func:`rebuild_with_named_vectors`. Raises
    :class:`MigrationRefused` when the class does not exist and
    :class:`vco_lib.weaviate_schema.SlotAddUnsupported` when the server
    refused the add (class unchanged in both).
    """
    schema = _wh.fetch_schema(name, weaviate_url=weaviate_url)
    if schema is None:
        raise MigrationRefused(
            f"collection {name!r} not found or its schema could not be read. "
            "Nothing was changed.")
    if not schema.get("vectorConfig"):
        return None
    return _ws.add_vector_slots_in_place(
        name, [_ws.NamedVectorSlot(n, int(d)) for n, d in slots.items()],
        weaviate_url=weaviate_url,
    )


def snapshot_rows(
    coll: Any,
    *,
    call_site: str,
    flat_vector_slot: str,
    weaviate_url: Optional[str] = None,
    counter: Optional[Callable[..., Optional[int]]] = None,
) -> List[Dict[str, Any]]:
    """Read every row of ``coll`` as ``{"uuid", "properties", "vectors"}``
    and prove the read whole against the server's own count.

    A flat (unnamed) vector is carried under ``flat_vector_slot``. Raises
    :class:`MigrationRefused` when the count cannot be read or differs from
    the number of rows read — the caller must then drop nothing.
    """
    name = str(getattr(coll, "name", "") or "")
    rows: List[Dict[str, Any]] = []
    for obj in iter_class(coll, call_site, return_properties=None,
                          include_vector=True, weaviate_url=weaviate_url):
        vec = obj.vector
        if isinstance(vec, dict):
            vectors = {k: v for k, v in vec.items() if v}
            if "default" in vectors:
                vectors[flat_vector_slot] = vectors.pop("default")
        elif vec:
            vectors = {flat_vector_slot: vec}
        else:
            vectors = {}
        rows.append({"uuid": obj.uuid, "properties": dict(obj.properties),
                     "vectors": vectors})
    count = (counter or _wh.http_count_objects)(name, weaviate_url)
    if count is None or count != len(rows):
        raise MigrationRefused(
            f"refusing to rebuild {name!r}: read {len(rows)} row(s) but the "
            f"server's count is {'unreadable' if count is None else count}. "
            "Nothing was dropped. Re-run when Weaviate answers the count and "
            "the listing agree.")
    return rows


def _clone_property(prop: Any) -> Any:
    from weaviate.classes.config import Property

    nested = getattr(prop, "nested_properties", None)
    kwargs: Dict[str, Any] = {
        "name": prop.name,
        "data_type": prop.data_type,
        "description": getattr(prop, "description", None),
    }
    if nested:
        kwargs["nested_properties"] = [_clone_property(p) for p in nested]
    return Property(**kwargs)


def rebuild_with_named_vectors(
    client: Any,
    name: str,
    slot_names: List[str],
    rows: List[Dict[str, Any]],
    *,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Drop ``name`` and recreate it with ``slot_names`` as named vectors,
    then re-insert ``rows`` (from :func:`snapshot_rows`) under their original
    uuids. Vectors for slots outside ``slot_names`` are not carried.

    ``index_null_state=True`` is set on the new class: the KG stale filter
    (``valid_until`` is-none) needs it and it cannot be added later.
    Returns the number of rows inserted.
    """
    from weaviate.classes.config import Configure

    from vco_lib.fixture_class_guard import record_collection_delete

    properties = [_clone_property(p)
                  for p in client.collections.get(name).config.get().properties]
    wanted = sorted(set(slot_names))
    record_collection_delete(client, name)
    client.collections.create(
        name=name,
        properties=properties,
        vectorizer_config=[Configure.NamedVectors.none(name=n) for n in wanted],
        inverted_index_config=Configure.inverted_index(index_null_state=True),
    )
    new_coll = client.collections.get(name)
    inserted = 0
    for row in rows:
        vectors = {k: v for k, v in row["vectors"].items() if k in wanted and v}
        new_coll.data.insert(
            properties=row["properties"],
            vector=vectors or None,
            uuid=row["uuid"],
        )
        inserted += 1
        if log is not None and inserted % 100 == 0:
            log(f"re-inserted {inserted}/{len(rows)} rows into {name!r}")
    return inserted
