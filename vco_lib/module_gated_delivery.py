# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Module-gated agent delivery: folder→project-id resolution + the gate.

Extracted from ``vco_lib/project_init.py`` (v0.2.96 WP-10). That module is
ratchet-capped and must SHRINK, not grow; the model-gateway delivery gate
landed there first and tripped the cap, which is the gate working — this
is the extraction it forced. Everything here is self-contained (reads the
launcher DB only; no project_init state) with three production callers:
the ``templates/agents/module-gateway/`` enumeration in
``_enumerate_bundle_files``, and the two folder→UUID resolutions that
previously inlined this pattern (``_apply_canonical_env_via_config_projection``
and ``_read_codegraph_binding_override``) — ONE home for the pattern, per
the one-concern-one-home rule.

Two facts this module exists to enforce:

* **The key divergence (survey LANE-WP10 §2, red-proofed in
  ``tests/test_conditional_template.py::ModuleGatewayDeliveryKeyTests``).**
  ``project_modules`` rows are keyed by the projects-table UUID
  (``commands/projects_v2.rs`` ``Uuid::new_v4()``; the toggle's re-render
  passes that UUID), while bundle-time paths historically resolved modules
  with ``str(folder)`` — a key that can never match a UUID row. A gate
  built on the folder key is green in every test that forgets to register
  the project and dead in production for every project that IS registered.
  ``resolve_project_id_for_folder`` is the folder→UUID half of the
  ``_apply_canonical_env_via_config_projection`` pattern, lifted out so the
  delivery gate and the env projections share one implementation.
* **The delivery gate is ACTIVE-keyed, not runnability-keyed** (survey §3).
  ``module_gateway_agents_active`` reads the ``project_modules`` row and
  nothing else — the same fact the CLAUDE.md routing-section toggle writes
  (GUI ``ROUTING_GUIDANCE_MODULE`` == :data:`GATEWAY_MODULE_NAME`), one row,
  two effects. It deliberately does NOT consult whether the gateway daemon
  is running, and the OFF lifecycle is the bundle engine's ordinary one
  (WP-10 review MAJOR-1, corrected 2026-09-21): a project that toggles OFF
  and updates has its UNMODIFIED delivered copies orphan-deleted by the
  manifest reconciliation, while USER-MODIFIED copies are kept and retired
  to the adoption backups — and a later toggle ON re-delivers. So OFF is
  self-healing, not sticky: the definitions do not linger on machines that
  renounce the module. The disclosure posture — the definitions'
  descriptions name the gateway requirement, and an unreachable id fails
  visibly at spawn — is the one the routing section already ships.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

# v0.2.96 L-11: the ONE home for the read-only `file:` URI. Built by hand
# here (and at ten other sites) until this release, which failed shut on any
# launcher.db path containing `?`, `#` or `%` — SQLite reads everything after
# the first `?` as the query string, so the path truncated and the gate
# silently never fired for that user.
from vco_lib.launcher_db_reader import sqlite_ro_uri

#: The module name shared by the GUI toggle, the CLAUDE.md template gate
#: (``{{#if_module_active model_gateway}}``) and this delivery gate. MUST
#: stay equal to the GUI's ``ROUTING_GUIDANCE_MODULE``
#: (``launcher/src/lib/api/model_gateway.ts``) — one ``project_modules``
#: row, three effects. Pinned by
#: ``tests/test_v0292_model_gateway_gui_contract.py``.
GATEWAY_MODULE_NAME = "model_gateway"

#: Source dir for model-gateway-gated agent definitions. NEVER ``free/`` —
#: that bucket ships UNCONDITIONALLY (see ``_enumerate_bundle_files``) and
#: would put hardcoded ``claude-gw/*`` frontmatter model ids on stock
#: installs that never opted into a gateway.
GATEWAY_AGENTS_DIR = "module-gateway"


def _launcher_db_path() -> Path:
    """Canonical launcher.db path (thin alias, same shape as project_init's)."""
    from vco_lib.paths import launcher_db_path as _canonical
    return _canonical()


def _canonical_path_eq(a: "str | Path", b: "str | Path") -> bool:
    """Delegate to project_init's canonical comparator (its single home)."""
    from vco_lib.project_init import _canonical_path_eq as _impl
    return _impl(a, b)


def resolve_project_id_for_folder(
    folder: Path,
    *,
    db_path: Path | None = None,
) -> Optional[str]:
    """Resolve a project FOLDER to its ``projects``-table id, read-only.

    Soft-fails to ``None`` on every miss (no DB, unreadable, table absent,
    folder not registered) — callers treat ``None`` as "cannot prove
    anything about this folder", never as an error.
    """
    target = db_path if db_path is not None else _launcher_db_path()
    if not target.is_file():
        return None
    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        conn = sqlite3.connect(
            sqlite_ro_uri(target), uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        try:
            return project_id_for_folder_on_conn(conn, folder_canonical)
        except sqlite3.Error:
            return None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def project_id_for_folder_on_conn(
    conn: sqlite3.Connection, folder_canonical: Path,
) -> Optional[str]:
    """The folder->id match itself, on a connection the caller owns.

    Unlike :func:`resolve_project_id_for_folder` this does NOT soft-fail: a
    query error propagates as :class:`sqlite3.Error`, so a caller that must
    tell "not registered" (``None``) from "could not ask" (the exception)
    can — ``vco_lib.parked_hooks`` is one. ``folder_canonical`` must already
    be resolved.
    """
    rows = conn.execute("SELECT id, folder_path FROM projects").fetchall()
    for row_id, row_folder in rows:
        if _canonical_path_eq(row_folder or "", folder_canonical):
            return str(row_id)
    return None


def module_gateway_agents_active(target_folder: Path) -> bool:
    """True iff the model_gateway module is ACTIVE for ``target_folder``.

    The delivery gate for ``templates/agents/module-gateway/`` — modelled
    on the ONE conditional file-delivery precedent the bundle engine has
    (``_is_root_bundle_target``, the v0.2.81 root-only knowledge gate) but
    keyed on the launcher.db module row instead of path identity.
    Conservative in the SAME direction: resolve failure, missing DB,
    unregistered folder, or any resolver exception → ``False`` → the gated
    agents are NOT shipped (fails toward less materialization, never
    toward putting ``claude-gw/*`` ids on an install that never opted in).
    """
    project_id = resolve_project_id_for_folder(target_folder)
    if project_id is None:
        return False
    try:
        from vco_lib.project_init import resolve_active_modules
        active = resolve_active_modules(project_id)
    except Exception:
        return False
    return GATEWAY_MODULE_NAME in active
