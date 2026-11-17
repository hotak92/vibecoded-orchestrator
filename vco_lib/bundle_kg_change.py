# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Did this bundle run change anything the project's KG sync ingests?

v0.2.107 (audit A-015 / INSTALL-02). The launcher queues a whole-tree
``kg-sync --all`` after a bundle update when the update changed knowledge
content. It used to decide that from the envelope's ``actions`` with a PATH
rule — any changed path under ``knowledge/`` or ``docs/`` — so a release that
edited ``knowledge/.node_embeddings.README.txt`` (a sidecar the sync never
reads) queued ``kg-sync --all`` in every project of a machine at once.

The fact belongs to the side that knows what the sync reads, so the envelope
now STATES it (rule A, one home, here):

* ``kg_content_changed`` — ``True`` iff at least one path in a
  content-changing bucket is a file the sync ingests;
* ``kg_content_changed_paths`` — those paths (sorted), so the launcher can say
  WHY it queued a sync.

The launcher reads the key when present and keeps its old path rule only for
an envelope without it (an older ``project_init``). An older launcher ignores
both keys (it reads ``actions`` only), so either pairing degrades to today's
behaviour, never to a skipped sync.

What the sync ingests (``templates/scripts/sync_knowledge_graph.py``):
``knowledge/**/*.md`` minus the excluded meta files
(:data:`vco_lib.kg_sync_drift.EXCLUDED_SYNC_BASENAMES`), and
``docs/**/*.md``. A path with a dot-prefixed component (``.node_formats.json``,
a ``.cache/`` dir) is a sidecar, never a node.
"""

from __future__ import annotations

from typing import Iterable, Mapping

#: Envelope keys written by :func:`record_kg_content_change`. The launcher
#: reads the same literals (``change_detect.rs``: ``ENVELOPE_KG_CONTENT_CHANGED_KEY``).
KG_CONTENT_CHANGED_KEY = "kg_content_changed"
KG_CONTENT_CHANGED_PATHS_KEY = "kg_content_changed_paths"

#: The action buckets whose membership means the run changed a file's bytes
#: on disk (deletion included). MUST MATCH ``change_detect.rs ::
#: BUNDLE_CONTENT_CHANGING_BUCKETS`` (the old-envelope fallback reads the same
#: buckets; ``tests/test_v02107_update_all_gating.py`` pins the pair).
CONTENT_CHANGING_BUCKETS: tuple[str, ...] = (
    "create",
    "overwrite",
    "always-overwrite",
    "orphan-deleted",
)

_SYNCED_ROOTS = ("knowledge", "docs")


def is_synced_kg_path(rel: str) -> bool:
    """True iff ``rel`` (project-relative) is a file the KG sync ingests."""
    norm = str(rel).replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    parts = [p for p in norm.split("/") if p]
    if len(parts) < 2 or parts[0] not in _SYNCED_ROOTS:
        return False
    if any(p.startswith(".") for p in parts[1:]):
        return False
    name = parts[-1]
    if not name.lower().endswith(".md"):
        return False
    if parts[0] == "knowledge":
        from vco_lib.kg_sync_drift import EXCLUDED_SYNC_BASENAMES

        if name in EXCLUDED_SYNC_BASENAMES:
            return False
    return True


def kg_changed_paths(actions: Mapping[str, Iterable[str]] | None) -> list[str]:
    """The changed paths the sync ingests, sorted and de-duplicated."""
    if not isinstance(actions, Mapping):
        return []
    found: set[str] = set()
    for bucket in CONTENT_CHANGING_BUCKETS:
        entries = actions.get(bucket) or ()
        for rel in entries:
            if isinstance(rel, str) and is_synced_kg_path(rel):
                found.add(rel.replace("\\", "/"))
    return sorted(found)


def record_kg_content_change(result: dict) -> None:
    """Write both envelope keys into a finished bundle ``result``. Never raises
    — a failure leaves the keys absent, and the launcher then applies its old
    (conservative) path rule."""
    try:
        paths = kg_changed_paths(result.get("actions"))
        result[KG_CONTENT_CHANGED_KEY] = bool(paths)
        result[KG_CONTENT_CHANGED_PATHS_KEY] = paths
    except Exception:  # noqa: BLE001 — never break a bundle run over a report key
        result.pop(KG_CONTENT_CHANGED_KEY, None)
        result.pop(KG_CONTENT_CHANGED_PATHS_KEY, None)


__all__ = [
    "CONTENT_CHANGING_BUCKETS",
    "KG_CONTENT_CHANGED_KEY",
    "KG_CONTENT_CHANGED_PATHS_KEY",
    "is_synced_kg_path",
    "kg_changed_paths",
    "record_kg_content_change",
]
