# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The count-gated staging swap behind ``migrate-collections``' ``copy`` action
(v0.2.107, DESTRUCTIVE-01).

Extracted from ``vco_lib/project_init.py`` (its line ratchet); project_init
keeps a thin ``_swap_through_staging`` shim. Every Weaviate call goes through
project_init's module-level helpers, resolved at CALL time, so the existing
``mock.patch.object(project_init, ...)`` test seams govern this module too.
"""
from __future__ import annotations

from typing import Optional


def swap_through_staging(
    name: str, target: dict, *, weaviate_url: Optional[str] = None,
) -> int:
    """Recreate ``name`` with the ``target`` schema, carrying every object
    (UUID, properties, named vectors) through ``<name>__staging``. Returns the
    object count carried. Used ONLY for the settings Weaviate cannot change on
    a live class (``indexNullState``, ``vectorIndexType``) - a missing vector
    slot is added in place and never comes here.

    v0.2.107 (DESTRUCTIVE-01) ordering - the live class is dropped ONLY after
    the staging copy is proven complete by a count the copier did not produce:

    1. A ``<name>__staging`` that already exists was not created by this run
       and may be the only copy of something: REFUSE, touch nothing.
    2. Read ``name``'s object count through the ONE count helper. ``None``
       (could not be read) -> REFUSE before anything is created.
    3. Create staging, copy ``name`` -> staging, read staging's count through
       the helper. The copier's tally AND staging's count must both equal the
       source count; on ``None`` or any mismatch the staging class this run
       created is removed, ``name`` is untouched, and the swap raises.
    4. Only then: drop ``name``, recreate it, copy staging -> ``name``.
    5. ``name``'s count (helper) and the copier's tally must equal the source
       count; otherwise raise with staging RETAINED - it holds the data.
    6. Drop staging.

    Not covered: a writer that adds rows to ``name`` between step 2 and
    step 4 (no writer exclusion exists); such rows make step 3 mismatch only
    if they land before the copy ends.
    """
    from vco_lib import project_init as _pi

    staging = f"{name}{_pi._STAGING_SUFFIX}"
    if _pi._fetch_schema(staging, weaviate_url=weaviate_url) is not None:
        raise RuntimeError(
            f"{staging} already exists and this run did not create it; it may "
            f"hold the only copy of {name}'s data. Refusing the swap - nothing "
            "was created, copied or dropped."
        )
    src_count = _pi._count_objects(name, weaviate_url=weaviate_url)
    if src_count is None:
        raise RuntimeError(
            f"the object count of {name} could not be read, so a copy of it "
            "could not be proven complete. Refusing the swap - nothing was "
            "created, copied or dropped."
        )
    staging_def = dict(target)
    staging_def["class"] = staging
    _pi._create_class(staging_def, weaviate_url=weaviate_url)
    try:
        copied_a = _pi._copy_collection_with_vectors(
            name, staging, weaviate_url=weaviate_url,
        )
        staging_count = _pi._count_objects(staging, weaviate_url=weaviate_url)
        if copied_a != src_count or staging_count != src_count:
            raise RuntimeError(
                f"staging copy of {name} is not proven complete: {name} holds "
                f"{src_count} object(s), the copy wrote {copied_a}, {staging} "
                f"counts {'unreadable' if staging_count is None else staging_count}. "
                f"{name} was NOT dropped."
            )
    except Exception:
        # The staging class is this run's own (step 1 refused a pre-existing
        # one) and `name` still holds everything: remove the partial copy.
        try:
            _pi._delete_class(staging, weaviate_url=weaviate_url)
        except Exception:  # noqa: BLE001 - the original error is the one to raise
            pass
        raise
    _pi._delete_class(name, weaviate_url=weaviate_url)
    _pi._create_class(target, weaviate_url=weaviate_url)
    copied_b = _pi._copy_collection_with_vectors(
        staging, name, weaviate_url=weaviate_url,
    )
    final_count = _pi._count_objects(name, weaviate_url=weaviate_url)
    if copied_b != src_count or final_count != src_count:
        raise RuntimeError(
            f"copy back into {name} is not proven complete: expected "
            f"{src_count} object(s), the copy wrote {copied_b}, {name} counts "
            f"{'unreadable' if final_count is None else final_count}. "
            f"{staging} RETAINED - it holds the data."
        )
    _pi._delete_class(staging, weaviate_url=weaviate_url)
    return copied_b
