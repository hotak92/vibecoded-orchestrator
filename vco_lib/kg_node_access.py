# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-node ``cross_project_access`` — THE read-side verdict (one home).

The WRITE side is the launcher's per-node access editor
(``kg_set_node_access`` / ``kg_set_node_access_bulk``, Rust
``commands/kg.rs``): it PATCHes ``cross_project_access: text[]`` onto the
Weaviate object — ``["*"]`` = readable by every project, ``[<project-id>…]``
= exactly those launcher project ids, ``[]`` = private.

Every KG retrieval surface (agent- and hook-facing; the operator maintenance
tools ``repair_kg_typed_links.py`` and ``maintain_knowledge_graph.py`` read
their own explicitly named collections and are out of scope) that can return a row from a collection that is not
the requesting project's own applies :func:`filter_objects` to those rows:

* the weaviate-kg MCP fan-out (``hybrid_search``, ``semantic_graph_search``
  incl. its WikiLink neighbour fetch, ``search_code_graph``) — through
  ``weaviate_mcp/server.py::_filter_cross_project_access``;
* the hook router's KG leg (``claude_mcp_servers/scripts/rl_kg_search.py``,
  used by every context-injection surface) — through the same server
  wrapper;
* the ``kg-search`` CLI (``templates/scripts/search_knowledge.py``:
  ``search`` / ``list`` / ``recent`` / ``created``) and the ``kg-info`` CLI
  (``templates/scripts/get_node_info.py``: ``info`` / ``connections``) —
  directly.

The launcher's KG screen states this next to the control
(``launcher/src/lib/kg/node-access-notice.ts``); that wording is only true
while every reader above routes through here.

Back-compat BOTH arms: a row that does not carry the property (legacy
classes, collections the editor never touched) is UNRESTRICTED, and the
SELF collection is never filtered (its rows are this project's own data;
the collection-level access matrix already governs which peers see the
collection at all). Each call site decides what "self" means.

Pure stdlib, no side effects at import — the CLIs import it cheaply.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Union

#: The Weaviate property the launcher's editor writes.
ACCESS_PROPERTY = "cross_project_access"

#: The env var the launcher projects into every surface (config_projection)
#: carrying this project's launcher id — the hub-independent identity rung.
PROJECT_ID_ENV = "VCT_PROJECT_ID"

Requester = Union[str, Callable[[], str]]


def allows(props: Any, requester_project_id: str) -> bool:
    """May ONE row from a non-self collection reach *requester_project_id*?

    ``props`` is the row's raw properties mapping (``None``-safe). The
    verdict table for ``cross_project_access``:

    * absent / ``None`` → ALLOW (legacy row, never restricted);
    * contains ``"*"`` → ALLOW (explicitly shared with every project);
    * non-empty list → ALLOW only when *requester_project_id* is in it;
    * empty list → DENY (the writer's ``private`` mode);
    * a value that is neither list/tuple nor ``None`` → DENY (a malformed
      restriction is still a restriction — never guess a row onto a list).

    A non-empty restriction with an UNRESOLVABLE requester id (``""``)
    DENIES: the row demands proof of identity and this process cannot
    present one.
    """
    if not isinstance(props, dict):
        return True  # unreadable properties never carried a restriction we can read
    allowed = props.get(ACCESS_PROPERTY)
    if allowed is None:
        return True
    if not isinstance(allowed, (list, tuple)):
        return False
    if "*" in allowed:
        return True
    if not allowed:
        return False
    return bool(requester_project_id) and requester_project_id in allowed


def _needs_identity(props: Any) -> bool:
    """True when the verdict for *props* depends on the requester id."""
    if not isinstance(props, dict):
        return False
    allowed = props.get(ACCESS_PROPERTY)
    return isinstance(allowed, (list, tuple)) and bool(allowed) and "*" not in allowed


def filter_objects(
    objects: Iterable[Any],
    *,
    is_self: bool,
    requester: Requester,
) -> list:
    """Apply the per-node verdict to ONE collection's Weaviate rows.

    ``is_self`` → returned unchanged (as a list). Otherwise every row whose
    ``.properties`` the verdict denies is dropped. ``requester`` is the
    project id or a zero-arg callable returning it; a callable is invoked
    at most once and ONLY when a row's verdict actually depends on the
    identity (so unrestricted fan-outs never pay a hub round-trip).
    """
    rows = list(objects or [])
    if is_self:
        return rows
    resolved: list[str] = []

    def _who() -> str:
        if not resolved:
            resolved.append(requester() if callable(requester) else (requester or ""))
        return resolved[0] or ""

    out = []
    for obj in rows:
        props = getattr(obj, "properties", None)
        who = _who() if _needs_identity(props) else ""
        if allows(props, who):
            out.append(obj)
    return out


def requester_from_config(cfg: Any) -> str:
    """This project's launcher id from a resolved ``ProjectConfig`` (or None).

    The restriction lists carry launcher project ids (the editor's GUI
    picker sends ``project.id``); the hub's ``ProjectConfig.project_id`` is
    the same id. When the hub cannot answer, the launcher-projected
    ``$VCT_PROJECT_ID`` (same namespace, written by ``config_projection``)
    is the second rung; ``""`` when neither is known (restricted rows are
    then denied, never guessed onto).
    """
    pid = (getattr(cfg, "project_id", "") or "") if cfg is not None else ""
    pid = str(pid).strip()
    if pid:
        return pid
    return (os.environ.get(PROJECT_ID_ENV) or "").strip()


@functools.lru_cache(maxsize=8)
def resolve_requester_project_id(project_root: "Path | str") -> str:
    """Resolve the requester id for a CLI rooted at *project_root*.

    Hub first (``vco_lib.project_config.resolve`` — which honours the
    ``VCT_DISABLE_HUB_RESOLVER`` gate), then ``$VCT_PROJECT_ID``. A hub that
    cannot answer is not an error here: the verdict denies restricted rows
    for an unknown requester, which is the conservative outcome.
    """
    cfg = None
    try:
        from vco_lib.project_config import resolve

        cfg = resolve(project_root)
    except Exception:
        cfg = None
    return requester_from_config(cfg)


def filter_cli_rows(
    objects: Iterable[Any],
    *,
    coll_name: str,
    self_collection: str,
    project_root: "Path | str",
) -> list:
    """The CLI shape: rows of *coll_name*, self when it equals
    *self_collection*, requester resolved (once per process and root) from
    *project_root* only when a row's verdict needs it."""
    root = str(project_root)
    return filter_objects(
        objects,
        is_self=(coll_name == self_collection),
        requester=lambda: resolve_requester_project_id(root),
    )
