# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Read a project's KG collection binding from ``launcher.db``.

One function: :func:`_read_kg_binding_override`. It is LIVE — imported by
``vco_lib.project_init`` and reached in production via ``_apply_standalone_env``
(the OSS / ``--write-env`` path that runs when the launcher-DB projection is
unavailable).

History (v0.2.92): this module was created to hold an extracted "D18
correction" — logic that rewrote a project's ``KG_COLLECTION`` env value when
it disagreed with the registered binding. Investigation established that the
premise was wrong: the live writer
(:func:`vco_lib.config_projection.apply_project_env`, reached through
``install_project_bundle``) already OVERWRITES canonical env keys from the
binding on every bundle update, so nothing was preserving a stale value. Worse,
when the *binding itself* is the stale thing — the pre-v0.2.89 half-rename
damage class — correcting the env file from that binding would have re-stamped
the ghost value on every run while reporting success.

The correction surface was therefore deleted with the user's approval rather
than wired anywhere, and this module keeps only the binding reader that was
always live. The binding-side remedy the original D18 entry said was
unconfirmed DOES exist — WITH ONE BOUNDARY (v0.2.92, Fable round 6):
``vco_lib.kg_binding_heal._prefix_adopt_kg_bindings_pass`` (selects
``project_kg_bindings`` role-UNFILTERED, so primary rows included) runs via
``install.py --update`` (via ``_self_heal_kg_bindings_on_update``) on every
update, and primary-role coverage of the ABSENT-ghost case is pinned by
``tests/test_self_heal_cross_prefix_adopt.py::test_t10_primary_role_ghost_binding_is_adopted``.
But the pass skips any row whose ``collection_name`` EXISTS in Weaviate
(``if coll_name in existing_classes: continue``) — it deliberately does not
choose between two existing same-suffix classes. A ghost that has already
received the project's writes exists (the MCP creates a class on first
store), so the prefix-adopt pass never touches THAT case.

v0.2.92 closes it in two halves. It is diagnosed read-only by
``vco_lib.kg_binding_doctor`` (the ``kg_binding_evidence_mismatch``
condition, emitted from ``vco doctor`` and the install/update end-of-run
doctor phase), and where the file-backed evidence is UNAMBIGUOUS — one class
clears the ownership bar with no rival to it (alone, or leading the runner-up
by the decisive margin) and it is not the bound one — ``kg_binding_heal``
re-points the binding at it on the next install/update
(``kg_binding_evidence_repointed``). A split with no decisive leader writes
nothing and is ASKED about (``kg_binding_ambiguous_evidence``); below-bar,
``manual_override`` and already-bound-elsewhere cases write nothing and stay
diagnosed for the human to repair from the launcher's Identity tab. The
heal's candidate comes from that measured evidence and NEVER from the
name-derived class — the R38 guess that re-stamps the ghost precisely when
the name is what went wrong.
"""
from __future__ import annotations

from pathlib import Path

# v0.2.96 (ship-gate D-4): the `_canonical_path_eq` import that used to sit
# here is gone with the inline folder→project-id loop it served — that step
# now calls `module_gated_delivery.resolve_project_id_for_folder`, the ONE
# home, which applies the same comparator internally.


def config_has_manual_override(config_json) -> bool:
    """True when a binding row's ``config_json`` carries a truthy
    ``manual_override`` sentinel — a deliberate human pick automation must
    never overwrite.

    THE one Python home for that predicate (v0.2.92 D18 heal). It was
    previously inline in :func:`_read_kg_binding_override` only; the
    evidence-backed repoint in :mod:`vco_lib.kg_binding_heal` needs the very
    same question answered, and two inline copies of a
    "may automation write here?" test is exactly the drift the modularity
    rule forbids — the second copy is where the guard silently stops
    matching the first.

    Accepts the raw ``config_json`` TEXT (or an already-parsed value):
    unparsable / non-object config is NOT an override, so a corrupt row is
    still healable rather than frozen forever.

    Python truthiness is the contract, and the Rust mirror
    (``project_state_populate::shared_kg_binding::has_manual_override``)
    is written against it: a present-but-empty string, ``false``, ``0``,
    ``[]``, ``{}`` and ``null`` are all NOT an override; anything else is.
    """
    import json as _json

    cfg = config_json
    if isinstance(cfg, (str, bytes, bytearray)):
        try:
            cfg = _json.loads(cfg or "{}")
        except (_json.JSONDecodeError, TypeError, ValueError):
            return False
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("manual_override"))


def _read_kg_binding_override(folder: Path) -> dict:
    """v0.2.30: same launcher.db read as `_read_kg_collection_from_launcher_db`,
    but ALSO returns whether each binding carries a `manual_override`
    sentinel in its `config_json`. The caller uses this to decide
    whether an existing-but-stale settings.json env value should be
    corrected (manual_override = yes → correct) or preserved
    (manual_override = no → leave alone, the user might have edited
    settings.json directly).

    Returns a dict with keys:
        primary_kg_collection: str | None
        primary_has_manual_override: bool
        shared_kg_collection: str | None
        shared_has_manual_override: bool

    Soft-fails to an empty/defaults dict on any error path. Path
    resolution + Windows-aware comparison match
    `_read_kg_collection_from_launcher_db`.
    """
    import sqlite3 as _sqlite3

    out: dict = {
        "primary_kg_collection": None,
        "primary_has_manual_override": False,
        "shared_kg_collection": None,
        "shared_has_manual_override": False,
    }

    # Path resolution delegated to `vco_lib.paths.launcher_db_path` (v0.2.40 F5).
    from vco_lib.paths import launcher_db_path
    db_path = launcher_db_path()

    if not db_path.is_file():
        return out

    # v0.2.96 (ship-gate D-4): the folder→project-id step goes through
    # `module_gated_delivery.resolve_project_id_for_folder` — the ONE home
    # WP-10 extracted for exactly this pattern in this cycle. This function
    # was its FOURTH inline copy (the module's own header lists only three
    # migrated call-sites), and an inline copy is how the comparator drifts:
    # the shared one is `_canonical_path_eq`, case-insensitive on Windows.
    from vco_lib.module_gated_delivery import resolve_project_id_for_folder

    project_id = resolve_project_id_for_folder(folder, db_path=db_path)
    if project_id is None:
        return out

    try:
        # v0.2.96 L-11: the ONE home for this URI — a hand-interpolated
        # path containing ?/#/% truncates at the first ? and fails shut.
        from vco_lib.launcher_db_reader import sqlite_ro_uri
        conn = _sqlite3.connect(
            sqlite_ro_uri(db_path), uri=True, timeout=2.0)
    except _sqlite3.Error:
        return out

    try:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT role, collection_name, config_json FROM project_kg_bindings "
                "WHERE project_id = ?",
                (project_id,),
            )
            for role, name, config_json in cur.fetchall():
                if not name:
                    continue
                # ONE home for the sentinel rule (see the module-level
                # predicate) — the heal pass asks the identical question.
                has_override = config_has_manual_override(config_json)
                if role == "primary":
                    out["primary_kg_collection"] = name
                    out["primary_has_manual_override"] = has_override
                elif role == "shared":
                    out["shared_kg_collection"] = name
                    out["shared_has_manual_override"] = has_override
        except _sqlite3.Error:
            return out
    finally:
        try:
            conn.close()
        except _sqlite3.Error:
            pass

    return out


