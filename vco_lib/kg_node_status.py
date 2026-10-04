# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home of the "is this knowledge node archived?" predicate (v0.2.101).

A node is ARCHIVED when either:

* a PATH segment is ``archive`` / ``.archive`` / ``_archive`` — an exact
  segment match, never a substring, so ``architecture/`` and
  ``archived-notes/`` are left alone; or
* its frontmatter ``status`` is ``archived`` / ``deprecated`` / ``superseded``
  (the nested ``metadata:`` dialect is honoured, matching
  ``sync_knowledge_graph.py``'s frontmatter normalisation).

Why this needs a shared home
----------------------------
The KG seed writes to Weaviate through ``sync_knowledge_graph.py``, which
SKIPS an archived node — so no ``content_hash`` is ever stored for it. The
seed's change check (:func:`vco_lib.install_weaviate._compute_on_disk_content_hashes`)
must apply the SAME rule: before v0.2.101 it walked every ``*.md`` with no
filter, and :func:`vco_lib.install_weaviate.content_hash_diff` counts a
missing stored hash as changed — so every archived node was re-listed as
"changed" and re-walked on every update (~80–97 files in the field). One
predicate, three consumers, one rule.

Consumers
---------
* ``templates/scripts/sync_knowledge_graph.py::_is_archived_node`` — a thin
  delegator to :func:`is_archived_node` (kept as a named wrapper for its
  call-sites and tests; the skip/delete behaviour around it is unchanged).
* :mod:`vco_lib.install_weaviate` — :func:`is_archived_content` on the seed's
  on-disk walk.
* :mod:`vco_lib.kg_sync_drift` keeps its OWN C-leg mirror of this rule
  (``is_archived_node(rel_parts, content)``) because it cannot import the
  sync script — that module resolves the hub and imports ``weaviate`` at
  module scope. The mirror is parity-pinned against this home by
  ``tests/test_v02101_seed_and_data_keys.py``; it is unchanged by v0.2.101.

Not touched: an archived node is still kept on disk (grep/read history) and
the sync still deletes any prior Weaviate row for it — this module only
decides the yes/no.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

import yaml

#: ``MUST MATCH`` ``sync_knowledge_graph.py::_is_archived_node``'s
#: ``_ARCHIVE_DIR_SEGMENTS`` (and ``vco_lib.kg_sync_drift.ARCHIVE_DIR_SEGMENTS``).
ARCHIVE_DIR_SEGMENTS: frozenset[str] = frozenset({"archive", ".archive", "_archive"})

#: ``MUST MATCH`` ``sync_knowledge_graph.py::_is_archived_node``'s status set
#: (and ``vco_lib.kg_sync_drift.ARCHIVED_STATUS_VALUES``). ``superseded`` was
#: added 2026-05-22: authors who write it mean "should disappear from KG
#: queries".
ARCHIVED_STATUS_VALUES: frozenset[str] = frozenset(
    {"archived", "deprecated", "superseded"}
)


def is_archived_path(file_path: "Path | str") -> Tuple[bool, str]:
    """Path leg only: an exact ``archive`` / ``.archive`` / ``_archive``
    segment. No read, no frontmatter."""
    parts = Path(file_path).parts
    hit = next((p for p in parts if p in ARCHIVE_DIR_SEGMENTS), None)
    if hit is not None:
        return True, f"path contains {hit!r} segment ({file_path})"
    return False, ""


def frontmatter_status(content: str) -> Optional[str]:
    """The frontmatter ``status`` string of *content*, or ``None``.

    Mirrors the sync script's read closely enough to be exact for the
    ``status`` key: YAML-parse the leading block, and — matching
    ``_normalise_frontmatter``'s nested-dialect promotion — fall back to a
    ``metadata.status`` when the top level does not declare ``status``. A
    missing block, unparseable YAML, or a non-string value is ``None``
    ("cannot tell" — the safe direction).
    """
    if not content.strip().startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    status = fm.get("status")
    if status is None:
        nested = fm.get("metadata")
        if isinstance(nested, dict):
            status = nested.get("status")
    return status if isinstance(status, str) else None


def is_archived_node(
    file_path: "Path | str", frontmatter: Optional[Mapping[str, Any]] = None
) -> Tuple[bool, str]:
    """``(is_archived, reason)`` — the path leg OR the ``frontmatter.status``
    value (already parsed; ``None`` checks the path only). Mirrors
    ``sync_knowledge_graph.py::_is_archived_node`` exactly."""
    archived, reason = is_archived_path(file_path)
    if archived:
        return True, reason
    if frontmatter is not None:
        status = frontmatter.get("status")
        if isinstance(status, str) and status.strip().lower() in ARCHIVED_STATUS_VALUES:
            return True, f"frontmatter status={status.strip().lower()!r}"
    return False, ""


def is_archived_content(file_path: "Path | str", content: str) -> Tuple[bool, str]:
    """``(is_archived, reason)`` for a caller holding the raw *content*.

    The path leg, then the frontmatter ``status`` read from *content* —
    what :mod:`vco_lib.install_weaviate`'s on-disk walk needs (it reads the
    bytes anyway, to hash them)."""
    return is_archived_node(file_path, {"status": frontmatter_status(content)})