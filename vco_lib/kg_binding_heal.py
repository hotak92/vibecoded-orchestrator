# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""KG-binding self-heal — the single Python writer for ``launcher.db`` KG
binding / access / codegraph-prefix repair (X-1, v0.2.73).

Single-writer contract
-----------------------
Before v0.2.73 the ~790-line ``install.py::_self_heal_kg_bindings_on_update``
accumulated FIVE layers of drift-repair (case rebind, access-row rebind,
cross-prefix adoption, W40 adoption uplift, access-parity backfill) inline in
the install mega-file. Every layer wrote ``project_kg_bindings`` /
``kg_collection_access`` directly, and the repair existed *because* those
tables drifted — the launcher (Rust) creates the rows with canonical casing,
but historical installs, renames and manual overrides left stale rows the
next ``--update`` had to reconcile.

This module is now the ONE Python home for that repair. The contract:

* The launcher (Rust ``project_state``/``projects_v2`` commands) is the
  authoritative *creator* of binding rows.
* This module is the ONLY Python code that *heals* (updates) those rows,
  and it does so through a single entry point, :func:`self_heal_kg_bindings`.
* ``install.py`` keeps a thin shim (``_self_heal_kg_bindings_on_update``)
  that injects its own ``launcher.db``/logging/migrate helpers and delegates
  here. The heal SQL lives here, nowhere else.
* ``tests/test_kg_binding_heal_single_writer.py`` lints that no other Python
  module UPDATEs those columns.

Dependency injection
---------------------
The heal touches launcher.db + Weaviate + the migrate-collections smart path,
all of which install.py already knows how to reach. Rather than import
install.py back (a circular import — install.py is the entry script), the
public entry takes callables:

    self_heal_kg_bindings(
        deferral_report,
        *,
        db_path,                 # resolved launcher.db Path
        weaviate_url,            # resolved base URL
        existing_classes,        # set[str] from /v1/schema
        existing_by_lower,       # {lower: canonical}
        log_event,               # (stage, level, msg, *, data=None) -> None
        connect_rw,              # (db_path, *, label) -> sqlite3.Connection
        run_adoption_uplifts,    # optional smart-path uplift callable
    )

install.py resolves launcher.db + Weaviate schema (the cheap detection pass
that decides whether the writer lock is even needed) and hands the resolved
values in. Behaviour is byte-for-byte the pre-extraction behaviour.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from typing import Callable, Optional

# Access-level privilege ranking for kg_collection_access dedup decisions.
_KG_ACCESS_RANK: dict[str, int] = {"none": 0, "read": 1, "write": 2}

# Cross-prefix self-heal — suffixes considered when probing Weaviate for a
# populated sibling under a different prefix. Mirrors
# ``vco_lib.project_init._KG_SUFFIXES``; kept here so install.py's detection
# pass and this module agree without a cross-import.
_KG_BINDING_PREFIX_ADOPT_SUFFIXES: tuple[str, ...] = (
    "_KnowledgeGraph",
    "_Development",
)


def _count_weaviate_class_objects(
    weaviate_url: str, class_name: str,
) -> Optional[int]:
    """Count objects in ``class_name`` via Weaviate's GraphQL Aggregate.

    Returns:
        int  — object count when the request succeeds (0 for empty class).
        None — Weaviate unreachable, malformed response, or HTTP error.
               Callers MUST treat ``None`` as "unknown" (not zero) so a
               transient network blip cannot cause a populated collection
               to look empty and miss adoption.

    Soft-fails throughout: never raises into the caller.
    """
    base = (weaviate_url or "http://localhost:8081").rstrip("/")
    # GraphQL injection guard: class_name comes from Weaviate's own schema
    # endpoint (we filter from existing_classes), so it's already safe. But
    # validate the shape anyway to fail-closed if a future caller passes
    # user input.
    if not class_name or not class_name.replace("_", "").isalnum():
        return None
    query = (
        "{ Aggregate { "
        f"{class_name} {{ meta {{ count }} }}"
        " } }"
    )
    try:
        data = json.dumps({"query": query}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/v1/graphql",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(  # noqa: S310 (localhost only)
            req, timeout=10,
        )
    except Exception:
        return None
    try:
        status = resp.getcode()
    except Exception:
        status = 0
    if status != 200:
        return None
    try:
        payload = json.loads(resp.read())
    except Exception:
        return None
    try:
        agg = payload.get("data", {}).get("Aggregate", {}) or {}
        rows = agg.get(class_name) or []
        if not rows:
            # Aggregate returns empty list for missing class.
            return 0
        meta = rows[0].get("meta") or {}
        count = meta.get("count")
        if isinstance(count, int):
            return count
    except Exception:
        return None
    return None


def _rebind_collection_names_to_on_disk_casing(
    cur,
    *,
    table: str,
    project_id_col: str,
    collection_name_col: str,
    existing_classes: set[str],
    existing_by_lower: dict[str, str],
    extra_select_cols: tuple[str, ...] = (),
    do_rebind: Callable[..., None],
    resolve_conflict: Optional[Callable[..., None]] = None,
) -> list[tuple]:
    """Generic helper: rebind a SQLite table's ``collection_name`` column to
    the on-disk Weaviate casing when a case-different sibling exists in
    ``existing_classes``.

    Algorithm (per row):
      1. ``SELECT project_id, collection_name, *extra_select_cols FROM <table>``.
      2. If ``collection_name`` is exact-match in ``existing_classes`` → skip.
      3. If no case-insensitive sibling in ``existing_by_lower`` → skip
         (genuine missing class; orphan-prune sync recreates lazily).
      4. Otherwise the row needs rebinding. When ``resolve_conflict`` is
         provided, the helper probes the SELECT-time row set for a
         ``(project_id, target_name)`` collision; on hit, delegates to
         ``resolve_conflict`` (which mutates the DB and appends to
         ``rebinds`` via the closure). Otherwise — and always for tables
         where the rebind can't violate a unique constraint — calls
         ``do_rebind`` for a straight UPDATE.

    The helper is SQL-shape-agnostic: callers own the exact ``UPDATE`` /
    ``DELETE`` statements via ``do_rebind`` / ``resolve_conflict`` so
    table-specific concerns (extra ``SET`` columns, natural-key shape,
    privilege rules) stay with the caller.

    Returns the audit list — caller-supplied via the closures — so the
    parent function can pull a final summary into the deferral entry.
    """
    rebinds: list[tuple] = []
    select_cols = (project_id_col, collection_name_col) + extra_select_cols
    cur.execute(
        f"SELECT {', '.join(select_cols)} FROM {table}"
    )
    rows = cur.fetchall()

    # Build conflict lookup once if conflict-resolution is enabled — keyed
    # by (project_id, name) with the FULL row tuple as the value so
    # resolve_conflict can read extras (e.g. access_level for the
    # privilege-rank decision).
    conflict_lookup: dict[tuple, tuple] = {}
    if resolve_conflict is not None:
        for row in rows:
            proj_id = row[0]
            coll = row[1]
            if proj_id and coll:
                conflict_lookup[(proj_id, coll)] = row

    for row in rows:
        proj_id = row[0]
        coll_name = row[1]
        if not coll_name:
            continue
        if coll_name in existing_classes:
            # Exact match — nothing to do.
            continue
        actual = existing_by_lower.get(coll_name.lower())
        if actual is None or actual == coll_name:
            # Genuinely missing OR already canonical (defensive — filtered
            # above for missing-from-existing_classes).
            continue

        conflict_row: Optional[tuple] = None
        if resolve_conflict is not None:
            conflict_row = conflict_lookup.get((proj_id, actual))

        if conflict_row is not None and resolve_conflict is not None:
            resolve_conflict(
                cur,
                project_id=proj_id,
                old_name=coll_name,
                new_name=actual,
                current_row=row,
                conflict_row=conflict_row,
                rebinds=rebinds,
            )
        else:
            do_rebind(
                cur,
                project_id=proj_id,
                old_name=coll_name,
                new_name=actual,
                row=row,
                rebinds=rebinds,
            )

    return rebinds


def _prefix_adopt_kg_bindings_pass(
    cur,
    *,
    existing_classes: set[str],
    existing_by_lower: dict[str, str],
    weaviate_url: str,
) -> dict[str, list]:
    """Second-pass cross-prefix adoption for ``project_kg_bindings``.

    The case-insensitive first pass (see
    :func:`_rebind_collection_names_to_on_disk_casing`) handles rows whose
    ``collection_name`` differs only in casing from a live Weaviate class.
    This second pass handles a different shape: a row whose
    ``collection_name`` is GENUINELY MISSING from Weaviate (and has no
    case-sibling), but where Weaviate holds a populated class under a
    *different prefix* but the *same suffix*.

    Algorithm (per binding row left un-aligned by pass 1):
      1. Skip rows whose ``collection_name`` is already in
         ``existing_classes`` (exact match — nothing to do).
      2. Skip rows whose ``collection_name.lower()`` is in
         ``existing_by_lower`` (pass 1 would have rebound this).
      3. Determine the suffix (``_KnowledgeGraph`` or ``_Development``).
         Rows whose advertised name doesn't end in a known suffix are
         skipped — we don't second-guess arbitrary user-set names.
      4. Probe ``existing_classes`` for every class ending in that suffix;
         query Weaviate Aggregate for row counts; filter to candidates with
         ``row_count > 0``.
      5. Decision:
           * Exactly one candidate → UPDATE the binding row, tag
                                     ``manual_override:v0.2.40-prefix-adopt``.
           * Multiple populated    → record for
                                     ``multi_candidate_prefix_adopt``
                                     deferral; DO NOT modify the row.
           * Zero candidates       → no-op (legitimate missing-class state).

    Idempotency: a second run finds the adopted ``collection_name`` in
    ``existing_classes`` (step 1) and short-circuits at the per-row skip.

    Returns a dict with two keys:
        adopts:           list of (project_id, role, old_name, new_name, row_count)
        multi_candidates: list of (project_id, role, old_name, [(cand, count), ...])

    Soft-fails on Weaviate errors per candidate: when row-count probing
    returns ``None`` for a candidate, that candidate is treated as
    "unknown" and skipped (never adopted blindly).
    """
    adopts: list[tuple[str, str, str, str, int]] = []
    multi_candidates: list[tuple[str, str, str, list[tuple[str, int]]]] = []

    cur.execute(
        "SELECT project_id, role, collection_name, config_json "
        "FROM project_kg_bindings"
    )
    rows = cur.fetchall()

    # Group existing classes by suffix once — we'll consult per row.
    classes_by_suffix: dict[str, list[str]] = {
        s: [] for s in _KG_BINDING_PREFIX_ADOPT_SUFFIXES
    }
    for cls in existing_classes:
        for suffix in _KG_BINDING_PREFIX_ADOPT_SUFFIXES:
            if cls.endswith(suffix):
                classes_by_suffix[suffix].append(cls)
                break

    # Cache row-counts so we don't re-probe Weaviate twice for the same
    # class when multiple binding rows map to the same suffix.
    count_cache: dict[str, Optional[int]] = {}

    def _count(name: str) -> Optional[int]:
        if name not in count_cache:
            count_cache[name] = _count_weaviate_class_objects(
                weaviate_url, name,
            )
        return count_cache[name]

    for row in rows:
        proj_id, role, coll_name, config_json = row
        if not coll_name:
            continue
        # Pass-1 already aligned exact-match and case-sibling rows.
        if coll_name in existing_classes:
            continue
        if coll_name.lower() in existing_by_lower:
            # Defensive: pass-1 should have rebound this. If it didn't,
            # leave the row alone — pass-1 owns that case.
            continue

        # Determine the suffix the row's name ends in. Unknown suffix →
        # skip (we don't probe arbitrary prefixes; the user might have a
        # custom convention we shouldn't second-guess).
        suffix: Optional[str] = None
        for s in _KG_BINDING_PREFIX_ADOPT_SUFFIXES:
            if coll_name.endswith(s):
                suffix = s
                break
        if suffix is None:
            continue

        # Find all populated candidate classes matching the suffix.
        candidates: list[tuple[str, int]] = []
        for cand in classes_by_suffix.get(suffix, []):
            if cand == coll_name:
                # Won't happen (we already filtered exact-match above),
                # but keep the guard defensively.
                continue
            cnt = _count(cand)
            if cnt is None:
                # Weaviate transient error or malformed response — skip
                # this candidate. Never auto-adopt with unknown count.
                continue
            if cnt > 0:
                candidates.append((cand, cnt))

        if not candidates:
            # No populated sibling under this suffix. Legitimate
            # missing-class state — preserve the existing contract
            # (orphan-prune sync recreates lazily; user picks via the
            # launcher's Shared KG picker).
            continue

        if len(candidates) == 1:
            # Exactly one populated sibling — auto-adopt.
            cand_name, cand_count = candidates[0]
            # Build the new config_json with the v0.2.40 sentinel.
            # Preserve other config_json keys so we don't clobber
            # user/launcher state in there.
            try:
                cfg = json.loads(config_json) if config_json else {}
                if not isinstance(cfg, dict):
                    cfg = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                cfg = {}
            cfg["manual_override"] = "v0.2.40-prefix-adopt"
            new_config = json.dumps(cfg)

            cur.execute(
                "UPDATE project_kg_bindings "
                "SET collection_name = ?, config_json = ?, updated_at = ? "
                "WHERE project_id = ? AND role = ?",
                (cand_name, new_config, int(time.time() * 1000), proj_id, role),
            )
            adopts.append((proj_id, role, coll_name, cand_name, cand_count))
        else:
            # Multiple populated candidates — refuse to guess. Sort by row
            # count descending so the deferral's listing leads with the
            # most populated candidate.
            candidates.sort(key=lambda c: c[1], reverse=True)
            multi_candidates.append((proj_id, role, coll_name, candidates))

    return {"adopts": adopts, "multi_candidates": multi_candidates}


def rebind_orchestrator_root_bindings(
    cur,
    *,
    project_id: str,
    canonical: str,
    now_ms: Optional[int] = None,
) -> None:
    """Rebind the orchestrator-root project's ``primary`` + ``shared`` KG
    binding rows to ``canonical`` (X-1 single-writer home).

    This is the canonical-casing rebind install.py runs when it detects the
    orchestrator-root install's shared-KG class settled on a canonical name
    different from the stored binding (V44-I). It was previously inline SQL
    in ``install.py``; moved here so ALL ``project_kg_bindings`` mutations
    live in one module.

    The caller owns the connection + commit (install.py takes an advisory
    lock around the whole install-state mutation block, of which this is one
    step, and commits after). ``now_ms`` defaults to the current epoch-ms.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    for role in ("primary", "shared"):
        cur.execute(
            "UPDATE project_kg_bindings SET collection_name = ?, updated_at = ? "
            "WHERE project_id = ? AND role = ?",
            (canonical, now_ms, project_id, role),
        )


#: app_state keys R8 converges. `orchestrator_root_kg_collection` is the pointer
#: `populate_kg_collection_access_for_project` reads (access.rs:1529);
#: `last_installed_shared_kg_collection` is the ACTUAL canonical shared name
#: install.py last seeded. On a white-label/rebind install the second changes
#: while the first stays at the machine default → the access seeder mints rows
#: for a class that doesn't exist in Weaviate.
_APP_STATE_ORCH_ROOT_KG = "orchestrator_root_kg_collection"
_APP_STATE_LAST_SHARED_KG = "last_installed_shared_kg_collection"


def _read_app_state(cur, key: str) -> str:
    """Read one app_state value (stripped), or '' when absent/table-missing."""
    try:
        cur.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = cur.fetchone()
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower():
            return ""
        raise
    return (row[0] or "").strip() if row else ""


def pointer_drift_needs_rw(ro_cur) -> bool:
    """R8 (v0.2.76): True when the two shared-KG app_state pointers DIVERGE.

    Cheap RO probe used by install.py's self-heal detection pass to decide
    whether the RW pass (which runs :func:`heal_shared_kg_pointer_drift`) is
    owed. A default install has them equal → False → no RW open. Older schemas
    without ``app_state`` / these rows read as absent → equal ('') → False.
    """
    try:
        ptr = _read_app_state(ro_cur, _APP_STATE_ORCH_ROOT_KG)
        last = _read_app_state(ro_cur, _APP_STATE_LAST_SHARED_KG)
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower():
            return False
        raise
    return ptr != last and bool(ptr or last)


def converge_root_pointer_write_side(
    db_path,
    canonical: str,
    *,
    is_root: bool,
    connect_rw: Callable[..., sqlite3.Connection],
    log_event: Callable[..., None],
    now_ms: Optional[int] = None,
) -> bool:
    """R8 (v0.2.76) write-side convergence: set
    ``app_state.orchestrator_root_kg_collection`` (the key the access seeder
    reads) to ``canonical`` when we've just seeded the canonical shared
    collection. ONLY the orchestrator-ROOT install may write it (``is_root``);
    a per-project install must never repoint it. Soft-fails; idempotent (same
    value → no-op UPSERT). Returns True when a write was issued.
    """
    canonical = (canonical or "").strip()
    if not canonical or not is_root:
        return False
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    try:
        conn = connect_rw(db_path, label="R8-pointer")
        try:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (_APP_STATE_ORCH_ROOT_KG, canonical, now_ms),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — soft-fail (telemetry-class write)
        return False
    log_event(
        "orchestrator_root_kg_collection", "ok",
        f"write-side convergence: pointer set to canonical shared collection "
        f"{canonical!r} (R8)",
        data={"value": canonical},
    )
    return True


def heal_shared_kg_pointer_drift(
    cur,
    *,
    existing_classes: set[str],
    log_event: Callable[..., None],
    deferral_report,
    deferral_entry_cls,
    now_ms: Optional[int] = None,
) -> int:
    """R8 (v0.2.76): converge ``orchestrator_root_kg_collection`` to the real
    canonical shared collection when it has drifted, and rewrite the stale
    ``kg_collection_access`` rows that point at the dead name.

    launcher.db-metadata ONLY: NO Weaviate object writes, NO sync enqueues, NO
    ``embed_revision`` changes. ``existing_classes`` is a read-only snapshot of
    the Weaviate schema (already fetched by the caller); the only Weaviate
    interaction anywhere in this heal is schema-existence membership, done by
    the caller.

    Convergence is DIVERGENCE-based with TRIPLE agreement (conservative):

      * ``ptr == last`` (keys agree) → strict no-op. This is the default-install
        path: both hold the machine default → nothing to do (leave-alone).
      * ``ptr != last`` AND all three agree:
          (1) ``last`` is non-empty,
          (2) ``last``'s class EXISTS in Weaviate,
          (3) ``last`` equals the consensus of ``role='shared'`` collection
              names in ``project_kg_bindings`` (all shared rows name the same
              collection, and it equals ``last``)
        → converge the pointer to ``last`` and rewrite stale
        ``kg_collection_access`` rows (see below).
      * ``ptr != last`` WITHOUT triple agreement → touch nothing; emit an
        ``UPDATE_DEFERRED``-pattern entry so the user resolves it explicitly.

    Access-row rewrite: only SEED-AUTHORED rows (``created_at == updated_at``,
    the migration-029 predicate) whose ``collection_name`` equals the dead
    ``ptr`` are rewritten to ``last``; user-configured rows are LEFT and
    reported. On PK conflict with an existing (project, last) row, keep the
    HIGHER privilege and delete the dead row.

    Idempotent: on an already-converged DB ``ptr == last`` and no stale rows
    remain → returns 0 with no writes (safe against the planner's manual
    repair). Returns the number of rows changed (pointer + access rewrites).
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    try:
        ptr = _read_app_state(cur, _APP_STATE_ORCH_ROOT_KG)
        last = _read_app_state(cur, _APP_STATE_LAST_SHARED_KG)
    except sqlite3.OperationalError:
        # app_state unreadable (older schema) — nothing to converge.
        return 0

    if not ptr and not last:
        return 0  # nothing recorded yet (pre-seed) — leave-alone.
    if ptr == last:
        return 0  # keys agree — default install / already converged.

    # Divergent. Require triple agreement before touching anything.
    reasons: list[str] = []
    if not last:
        reasons.append("last_installed_shared_kg_collection is empty")
    elif last not in existing_classes:
        reasons.append(
            f"last_installed value {last!r} is not a live Weaviate class"
        )

    shared_consensus: Optional[str] = None
    if not reasons:
        try:
            cur.execute(
                "SELECT DISTINCT collection_name FROM project_kg_bindings "
                "WHERE role = 'shared' AND collection_name IS NOT NULL "
                "AND collection_name != ''"
            )
            shared_names = {r[0] for r in cur.fetchall() if r and r[0]}
        except sqlite3.OperationalError as oe:
            if "no such table" in str(oe).lower():
                shared_names = set()
            else:
                raise
        if len(shared_names) == 1:
            shared_consensus = next(iter(shared_names))
        if shared_consensus is None:
            reasons.append(
                "no single-collection consensus among role='shared' bindings "
                f"(found {sorted(shared_names)})"
            )
        elif shared_consensus != last:
            reasons.append(
                f"shared-binding consensus {shared_consensus!r} != "
                f"last_installed {last!r}"
            )

    if reasons:
        # Diverge without agreement → touch nothing, defer to the user.
        log_event(
            "7e/10", "warn",
            "[kg-heal] shared-KG pointer drift NOT auto-converged "
            f"(ptr={ptr!r}, last={last!r}): {'; '.join(reasons)}",
            data={"ptr": ptr, "last": last, "reasons": reasons},
        )
        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="shared_kg_pointer_drift_unresolved",
                title="Shared-KG canonical pointer diverged (manual review)",
                detected=(
                    f"app_state.orchestrator_root_kg_collection = {ptr!r} but "
                    f"app_state.last_installed_shared_kg_collection = {last!r}. "
                    "The two disagree and the safe-convergence preconditions "
                    f"were not met: {'; '.join(reasons)}. The access seeder "
                    "reads the first key; a wrong value seeds kg_collection_access "
                    "rows for a class that may not exist."
                ),
                why_deferred=(
                    "Auto-converging without triple agreement (last value's "
                    "class exists in Weaviate AND matches the role='shared' "
                    "binding consensus) could re-point the access matrix at the "
                    "wrong collection. Left untouched pending explicit choice."
                ),
                command_to_apply=(
                    "Pick the canonical shared collection in the launcher's "
                    "Identity tab -> 'Manage shared KG collection', OR set "
                    "VCT_ORCHESTRATOR_ROOT_KG_COLLECTION and re-run "
                    "`python install.py --update`."
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )
        return 0

    # Triple agreement met → converge the pointer.
    changed = 0
    cur.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (_APP_STATE_ORCH_ROOT_KG, last, now_ms),
    )
    changed += 1
    log_event(
        "7e/10", "ok",
        f"[kg-heal] converged orchestrator_root_kg_collection {ptr!r} -> {last!r}",
        data={"old": ptr, "new": last},
    )

    # Rewrite stale kg_collection_access rows pointing at the dead ptr name.
    # SEED-AUTHORED rows only (created_at == updated_at per migration 029).
    try:
        cur.execute(
            "SELECT project_id, access_level, created_at, updated_at "
            "FROM kg_collection_access WHERE collection_name = ?",
            (ptr,),
        )
        dead_rows = cur.fetchall()
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower():
            dead_rows = []
        else:
            raise

    user_configured_left = 0
    for proj_id, level, created_at, updated_at in dead_rows:
        if created_at != updated_at:
            # User-configured row — never auto-rewrite; report it.
            user_configured_left += 1
            log_event(
                "7e/10", "warn",
                "[kg-heal] leaving user-configured kg_collection_access row at "
                f"dead collection {ptr!r} (project={proj_id}); rewrite skipped",
                data={"project_id": proj_id, "dead": ptr, "canonical": last},
            )
            continue
        # Seed-authored → rewrite to canonical. Handle PK (project_id,
        # collection_name) conflict with an existing (proj, last) row:
        # keep the HIGHER privilege, delete the dead row.
        cur.execute(
            "SELECT access_level FROM kg_collection_access "
            "WHERE project_id = ? AND collection_name = ?",
            (proj_id, last),
        )
        existing = cur.fetchone()
        if existing is not None:
            keep = max(
                (level, existing[0]),
                key=lambda lv: _KG_ACCESS_RANK.get(lv, 0),
            )
            cur.execute(
                "UPDATE kg_collection_access SET access_level = ?, updated_at = ? "
                "WHERE project_id = ? AND collection_name = ?",
                (keep, updated_at, proj_id, last),
            )
            cur.execute(
                "DELETE FROM kg_collection_access "
                "WHERE project_id = ? AND collection_name = ?",
                (proj_id, ptr),
            )
        else:
            # No conflict — preserve the seed-authored timestamps so the row
            # stays seed-authored (created_at == updated_at) under the canonical
            # name.
            cur.execute(
                "UPDATE kg_collection_access SET collection_name = ? "
                "WHERE project_id = ? AND collection_name = ?",
                (last, proj_id, ptr),
            )
        changed += 1

    if dead_rows:
        log_event(
            "7e/10", "ok",
            f"[kg-heal] rewrote {changed - 1} stale kg_collection_access row(s) "
            f"{ptr!r} -> {last!r}"
            + (f"; left {user_configured_left} user-configured row(s)"
               if user_configured_left else ""),
            data={
                "rewritten": changed - 1,
                "user_configured_left": user_configured_left,
                "dead": ptr, "canonical": last,
            },
        )
    return changed


def self_heal_kg_bindings(
    deferral_report,
    *,
    db_path,
    weaviate_url: str,
    existing_classes: set[str],
    existing_by_lower: dict[str, str],
    log_event: Callable[..., None],
    connect_rw: Callable[..., sqlite3.Connection],
    deferral_entry_cls,
    run_adoption_uplifts: Optional[Callable[..., None]] = None,
) -> None:
    """Heal launcher.db KG bindings / access rows against on-disk Weaviate.

    This is the extracted body of install.py's former
    ``_self_heal_kg_bindings_on_update`` RW pass (v0.2.23 B1 + v0.2.40
    cross-prefix adoption + V0243-9 access parity). The caller (install.py
    shim) has already:

      * resolved ``db_path`` (``_discover_app_state_db_path``) and confirmed
        the file exists,
      * fetched the Weaviate schema and built ``existing_classes`` /
        ``existing_by_lower``,
      * run the cheap RO detection pass and decided a rebind is owed.

    So this function opens launcher.db RW (via ``connect_rw``), runs the four
    repair passes, commits, and emits deferral entries. It never raises:
    sqlite errors soft-fail to a ``kg_binding_self_heal_db_error`` deferral;
    the caller keeps a clean ``--update`` exit either way.

    ``deferral_entry_cls`` is the caller's ``DeferralEntry`` dataclass
    (injected to avoid this module importing install.py). ``connect_rw`` is
    the caller's retry-with-backoff connector. ``run_adoption_uplifts``, when
    provided, is the smart-path uplift (W40 RT-13) invoked after prefix
    adoptions.
    """
    import sqlite3

    rebinds: list[tuple[str, str, str, str]] = []  # (proj_id, role, old, new)
    access_rebinds: list[tuple[str, str, str]] = []  # (proj_id, old, new)
    # cross-prefix adoption (second pass) — see _prefix_adopt_kg_bindings_pass.
    # adopts:           (project_id, role, old_name, new_name, row_count)
    # multi_candidates: (project_id, role, old_name, [(cand_name, row_count), ...])
    prefix_adopts: list[tuple[str, str, str, str, int]] = []
    prefix_multi_candidates: list[tuple[str, str, str, list[tuple[str, int]]]] = []
    try:
        # retry-with-backoff on a transient lock (defense in depth alongside
        # the launcher closing its managed connection + pollers standing
        # down). Exhausted retries re-raise → the outer ``except
        # sqlite3.Error`` below writes the same deferral as before.
        conn = connect_rw(db_path, label="7e/10")
        try:
            cur = conn.cursor()

            # ── 1. project_kg_bindings ────────────────────────────────
            # Natural key (project_id, role) is unaffected by a
            # collection_name rebind, so no conflict-resolver is needed.
            def _bind_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
                role = row[2]
                cur.execute(
                    "UPDATE project_kg_bindings "
                    "SET collection_name = ?, updated_at = ? "
                    "WHERE project_id = ? AND role = ?",
                    (new_name, int(time.time() * 1000), project_id, role),
                )
                rebinds.append((project_id, role, old_name, new_name))

            try:
                binding_rebinds = _rebind_collection_names_to_on_disk_casing(
                    cur,
                    table="project_kg_bindings",
                    project_id_col="project_id",
                    collection_name_col="collection_name",
                    existing_classes=existing_classes,
                    existing_by_lower=existing_by_lower,
                    extra_select_cols=("role",),
                    do_rebind=_bind_rebind,
                )
            except sqlite3.OperationalError as oe:
                if "no such table" in str(oe).lower():
                    log_event(
                        "7e/10", "skip",
                        "project_kg_bindings table absent; nothing to self-heal",
                    )
                    return
                raise
            rebinds.extend(binding_rebinds)

            # ── 2. kg_collection_access ───────────────────────────────
            # Also rebind ``kg_collection_access`` rows whose
            # ``collection_name`` differs only in case from an on-disk
            # class. Without this, the launcher GUI's Identity tab access
            # matrix would render rows pointing at a class that doesn't
            # exist post-rename (and dangle), and the hub's
            # ``kg_access_list`` construction in config_api would see both
            # the lowercase-c grant AND the (implicit-fallback) capital-C
            # grant — confusing, and a silently-missed
            # ``access_level='none'`` signal if the user had explicitly
            # downgraded the lowercase-c entry.
            #
            # PK collision handling: kg_collection_access PK is
            # (project_id, collection_name). If (p1, "Foo", "read") exists
            # AND (p1, "foo", "write") also exists, a naive rebind would
            # violate the UNIQUE constraint. The helper detects the
            # collision before the UPDATE; on collision we KEEP the
            # higher-privilege row (write > read > none) at the canonical
            # casing and DELETE the lower-privilege duplicate.
            def _access_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
                cur.execute(
                    "UPDATE kg_collection_access "
                    "SET collection_name = ? "
                    "WHERE project_id = ? AND collection_name = ?",
                    (new_name, project_id, old_name),
                )
                rebinds.append((project_id, old_name, new_name))

            def _access_resolve_conflict(
                cur, *, project_id, old_name, new_name,
                current_row, conflict_row, rebinds,
            ):
                # current_row has access_level at index 2 (extra_select_cols).
                # conflict_row likewise.
                current_access = current_row[2]
                conflict_access = conflict_row[2]
                current_rank = _KG_ACCESS_RANK.get(current_access, 0)
                conflict_rank = _KG_ACCESS_RANK.get(conflict_access, 0)
                if current_rank > conflict_rank:
                    # Lowercase-c row is higher-privilege — drop the
                    # canonical-casing duplicate, then rebind.
                    cur.execute(
                        "DELETE FROM kg_collection_access "
                        "WHERE project_id = ? AND collection_name = ?",
                        (project_id, new_name),
                    )
                    cur.execute(
                        "UPDATE kg_collection_access "
                        "SET collection_name = ? "
                        "WHERE project_id = ? AND collection_name = ?",
                        (new_name, project_id, old_name),
                    )
                    rebinds.append((project_id, old_name, new_name))
                else:
                    # Canonical row has equal-or-higher privilege. Drop the
                    # lowercase-c row.
                    cur.execute(
                        "DELETE FROM kg_collection_access "
                        "WHERE project_id = ? AND collection_name = ?",
                        (project_id, old_name),
                    )
                    rebinds.append(
                        (project_id, old_name, f"{new_name} (deduped)")
                    )

            try:
                acc_rebinds = _rebind_collection_names_to_on_disk_casing(
                    cur,
                    table="kg_collection_access",
                    project_id_col="project_id",
                    collection_name_col="collection_name",
                    existing_classes=existing_classes,
                    existing_by_lower=existing_by_lower,
                    extra_select_cols=("access_level",),
                    do_rebind=_access_rebind,
                    resolve_conflict=_access_resolve_conflict,
                )
                access_rebinds.extend(acc_rebinds)
            except sqlite3.OperationalError as oe:
                # Older launcher.db schemas may not have kg_collection_access.
                # Don't fail the binding heal — just skip the access part.
                if "no such table" not in str(oe).lower():
                    raise

            # ── 3. cross-prefix adoption (SECOND PASS) ─────────────────
            # The case-insensitive sweep above handles the casing-flip
            # scenarios. It explicitly leaves rows alone when the
            # advertised ``collection_name`` doesn't exist in Weaviate AND
            # has no case-different sibling — the "genuine missing-class"
            # branch. This pass covers a different breakage shape: a
            # manual_override cleanup updated the PRIMARY binding to a
            # custom prefix but left the SHARED binding pointing at the
            # canonical release-default prefix, so the advertised shared
            # collection never gets populated and the actual data lives
            # under the per-project prefix. Probe Weaviate for
            # *_KnowledgeGraph / *_Development classes with non-zero row
            # count; exactly ONE candidate → auto-adopt; multiple → defer;
            # zero → no-op.
            try:
                prefix_outcomes = _prefix_adopt_kg_bindings_pass(
                    cur,
                    existing_classes=existing_classes,
                    existing_by_lower=existing_by_lower,
                    weaviate_url=weaviate_url,
                )
                prefix_adopts.extend(prefix_outcomes["adopts"])
                prefix_multi_candidates.extend(
                    prefix_outcomes["multi_candidates"]
                )
            except sqlite3.OperationalError as oe:
                # Already-handled ``no such table`` from the first pass; if
                # we got here that table exists. Re-raise other operational
                # errors — they're real corruption signals.
                if "no such table" not in str(oe).lower():
                    raise

            # ── 4. V0243-9: kg_collection_access parity self-heal ─────
            #
            # For every row in project_kg_bindings that lacks a matching
            # kg_collection_access row, INSERT-OR-IGNORE the canonical
            # access-level:
            #   role="primary"  → access_level="write"
            #   role="shared"   → access_level="read"
            #   role="archive"  → access_level="read"  (Development)
            #
            # Also backfill the matching _Development collection row: for
            # each primary KG binding whose collection ends in
            # "_KnowledgeGraph", derive the sibling "_Development"
            # collection name and INSERT-OR-IGNORE a "write" row.
            #
            # INSERT-OR-IGNORE is safe: the PK is (project_id,
            # collection_name). We never lower an existing privilege.
            _parity_inserts = 0
            try:
                # Read all existing kg_collection_access rows for lookup.
                cur.execute(
                    "SELECT project_id, collection_name, access_level "
                    "FROM kg_collection_access"
                )
                existing_access: set[tuple[str, str]] = {
                    (r[0], r[1]) for r in cur.fetchall()
                }
                # Read all project_kg_bindings rows.
                cur.execute(
                    "SELECT project_id, role, collection_name "
                    "FROM project_kg_bindings"
                )
                binding_rows = cur.fetchall()

                _ROLE_LEVEL = {"primary": "write", "shared": "read", "archive": "read"}
                # kg_collection_access schema has: project_id,
                # collection_name, access_level, created_at, updated_at.
                # These INSERTs are seed-path writes (the parity self-heal
                # asserts the system's default; they are NOT user-driven),
                # so we bind both timestamps to the SAME value. This
                # preserves the seed-path invariant ``created_at ==
                # updated_at`` so the Rust-side ``KgAccessRow::
                # is_user_configured`` predicate reads FALSE for rows we
                # land here.
                _seed_ts_ms = int(time.time() * 1000)

                for proj_id, role, coll_name in binding_rows:
                    if not coll_name:
                        continue
                    level = _ROLE_LEVEL.get(role, "read")
                    if (proj_id, coll_name) not in existing_access:
                        cur.execute(
                            "INSERT OR IGNORE INTO kg_collection_access "
                            "(project_id, collection_name, access_level, "
                            " created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (proj_id, coll_name, level,
                             _seed_ts_ms, _seed_ts_ms),
                        )
                        if cur.rowcount:
                            existing_access.add((proj_id, coll_name))
                            _parity_inserts += 1

                    # Backfill the sibling _Development collection for
                    # primary bindings.
                    if role == "primary" and coll_name.endswith("_KnowledgeGraph"):
                        dev_name = coll_name[:-len("_KnowledgeGraph")] + "_Development"
                        if (proj_id, dev_name) not in existing_access:
                            cur.execute(
                                "INSERT OR IGNORE INTO kg_collection_access "
                                "(project_id, collection_name, access_level, "
                                " created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (proj_id, dev_name, "write",
                                 _seed_ts_ms, _seed_ts_ms),
                            )
                            if cur.rowcount:
                                existing_access.add((proj_id, dev_name))
                                _parity_inserts += 1

                if _parity_inserts:
                    log_event(
                        "7e/10", "ok",
                        f"V0243-9: inserted {_parity_inserts} missing "
                        f"kg_collection_access row(s) (parity self-heal)",
                        data={"inserts": _parity_inserts},
                    )
            except sqlite3.OperationalError as oe:
                if "no such table" not in str(oe).lower():
                    raise
                # kg_collection_access table absent — older schema; skip.
                log_event(
                    "7e/10", "skip",
                    "V0243-9: kg_collection_access absent; parity self-heal skipped",
                )

            # ── 5. R8 (v0.2.76): shared-KG canonical pointer drift ────
            # Converge app_state.orchestrator_root_kg_collection to the real
            # canonical shared collection + rewrite stale kg_collection_access
            # rows. launcher.db metadata only (no Weaviate writes). Conservative:
            # a default install has ptr == last → no-op; divergence without
            # triple agreement defers instead of guessing.
            _pointer_changed = 0
            try:
                _pointer_changed = heal_shared_kg_pointer_drift(
                    cur,
                    existing_classes=existing_classes,
                    log_event=log_event,
                    deferral_report=deferral_report,
                    deferral_entry_cls=deferral_entry_cls,
                )
            except sqlite3.OperationalError as oe:
                if "no such table" not in str(oe).lower():
                    raise
                log_event(
                    "7e/10", "skip",
                    "R8: app_state absent; shared-KG pointer heal skipped",
                )

            conn.commit()

            # X-1 instrumentation (v0.2.76): count rows this heal pass
            # ACTUALLY changed. This is the KPI that lets a future release
            # demote the heal to assert-only: once ``changed`` stays 0 across
            # releases, the launcher (Rust) creator path is provably keeping
            # the binding rows canonical and the reconciliation pass is dead
            # weight. Reuses the accumulators already tracked above — no new
            # telemetry machinery. ``rebinds`` also feeds the deferral/report
            # blocks below, so this is a pure read of existing state.
            _heal_changed = (
                len(rebinds)
                + len(access_rebinds)
                + len(prefix_adopts)
                + _parity_inserts
                + _pointer_changed
            )
            log_event(
                "7e/10", "ok",
                f"[kg-heal] changed={_heal_changed} "
                f"(binding_rebinds={len(rebinds)}, "
                f"access_rebinds={len(access_rebinds)}, "
                f"prefix_adopts={len(prefix_adopts)}, "
                f"access_parity_inserts={_parity_inserts}, "
                f"pointer_drift={_pointer_changed})",
                data={
                    "changed": _heal_changed,
                    "binding_rebinds": len(rebinds),
                    "access_rebinds": len(access_rebinds),
                    "prefix_adopts": len(prefix_adopts),
                    "access_parity_inserts": _parity_inserts,
                    "pointer_drift": _pointer_changed,
                },
            )
        finally:
            conn.close()
    except sqlite3.Error as se:
        log_event(
            "7e/10", "warn",
            f"launcher.db sqlite error during self-heal: {type(se).__name__}",
            data={"db_path": str(db_path), "error": str(se)[:200]},
        )
        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="kg_binding_self_heal_db_error",
                title="Could not self-heal launcher.db KG bindings (sqlite error)",
                detected=(
                    f"Tried to open launcher.db at {db_path} to detect "
                    f"case-mismatched `project_kg_bindings` rows, but the "
                    f"sqlite library raised {type(se).__name__}. The binding "
                    f"rows (if any) were NOT modified."
                ),
                why_deferred=(
                    "The launcher.db file is locked, corrupted, or "
                    "schema-mismatched. Skipping self-heal preserves user "
                    "state; the launcher's own boot path will re-validate "
                    "the schema on next start."
                ),
                command_to_apply=(
                    "Close the launcher if running, then re-run "
                    "`python install.py --update`. If the error persists, "
                    "open the launcher and let it migrate the schema first, "
                    "then re-run the update."
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )
        return

    if (
        not rebinds
        and not access_rebinds
        and not prefix_adopts
        and not prefix_multi_candidates
    ):
        log_event(
            "7e/10", "ok",
            "no case-mismatched KG bindings or access rows; self-heal no-op",
        )
        return

    # RT-13: W40-adoption smart-path uplift.
    #
    # After each binding flip has been committed, run the
    # migrate_collections smart-path against every newly-adopted collection
    # to detect schema drift between the legacy-named collection and the
    # current orchestrator target schema. Applied BEFORE the deferral-entry
    # block so any rebuild deferrals are merged into the same
    # UPDATE_DEFERRED.md write. Soft-fail: migrate errors become deferral
    # entries; the binding flip already committed so this step cannot roll
    # back the adoption.
    if prefix_adopts and run_adoption_uplifts is not None:
        run_adoption_uplifts(
            prefix_adopts=prefix_adopts,
            weaviate_url=weaviate_url,
            deferral_report=deferral_report,
            db_path=db_path,
        )

    # Emit deferral entries. Case-rebinds + prefix-adopts share an
    # informational ``kg_binding_self_healed`` entry because both are
    # auto-applied metadata fixes (the target class exists in Weaviate
    # before we point the binding at it). Multi-candidate prefix situations
    # get a SEPARATE ``multi_candidate_prefix_adopt`` entry with warning
    # severity — they require user input.
    binding_count = len(rebinds)
    access_count = len(access_rebinds)
    prefix_adopt_count = len(prefix_adopts)
    if rebinds or access_rebinds or prefix_adopts:
        rebind_lines = "\n".join(
            f"  * project_id={pid} role={role}: `{old}` → `{new}`"
            for (pid, role, old, new) in rebinds
        )
        access_rebind_lines = "\n".join(
            f"  * project_id={pid}: `{old}` → `{new}`"
            for (pid, old, new) in access_rebinds
        )
        prefix_adopt_lines = "\n".join(
            f"  * project_id={pid} role={role}: `{old}` → `{new}` "
            f"(adopted populated class with {cnt} object(s))"
            for (pid, role, old, new, cnt) in prefix_adopts
        )
        title_parts = []
        if binding_count:
            title_parts.append(f"{binding_count} case-rebound binding(s)")
        if access_count:
            title_parts.append(f"{access_count} access row(s)")
        if prefix_adopt_count:
            title_parts.append(
                f"{prefix_adopt_count} cross-prefix adopted binding(s)"
            )
        title = (
            f"Self-healed {' + '.join(title_parts)} in launcher.db KG metadata"
        )
        detected_parts = []
        if binding_count:
            detected_parts.append(
                f"Found {binding_count} `project_kg_bindings` row(s) whose "
                f"`collection_name` differed only in casing from a class that "
                f"exists in Weaviate at {weaviate_url}.\n\n"
                f"Rebound binding rows:\n{rebind_lines}"
            )
        if access_count:
            detected_parts.append(
                f"Found {access_count} `kg_collection_access` row(s) whose "
                f"`collection_name` differed only in casing from a class that "
                f"exists in Weaviate (sibling rows to the binding rebinds). "
                f"These were updated in place to keep the launcher GUI's "
                f"per-project Identity tab access matrix pointing at the live "
                f"class. Rows annotated `(deduped)` were merged with a pre-"
                f"existing canonical-casing row at equal-or-higher privilege.\n\n"
                f"Rebound access rows:\n{access_rebind_lines}"
            )
        if prefix_adopt_count:
            detected_parts.append(
                f"Found {prefix_adopt_count} `project_kg_bindings` row(s) "
                f"whose advertised `collection_name` does not exist in "
                f"Weaviate, but a SINGLE populated class under a different "
                f"prefix matches the same suffix "
                f"(`_KnowledgeGraph` / `_Development`). Auto-adopted the "
                f"populated class and tagged `config_json` with "
                f"`manual_override: v0.2.40-prefix-adopt` so downstream "
                f"env-backfill picks up the new collection name on the next "
                f"`populate()` call.\n\n"
                f"Adopted bindings:\n{prefix_adopt_lines}"
            )
        detected = "\n\n".join(detected_parts) + "\n\nNo data was touched."

        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="kg_binding_self_healed",
                title=title,
                detected=detected,
                why_deferred=(
                    "This is an informational entry — the heal was applied "
                    "automatically (it's a metadata fix, not a destructive "
                    "operation, since the target class already exists in "
                    "Weaviate). The launcher.db row(s) now match the actual "
                    "Weaviate class names so writes/reads route to the live "
                    "class instead of a nonexistent variant.\n\n"
                    "Background: install.py v0.2.23 B1 (2026-05-21) flipped "
                    "the canonical shared-KG class name from "
                    "`VibecodedOrchestrator_KnowledgeGraph` (lowercase c) "
                    "to `VibeCodedOrchestrator_KnowledgeGraph` (capital C, "
                    "matching the brand spelling). Case-insensitive adoption "
                    "in `_ensure_collections` keeps the on-disk casing "
                    "unchanged; this helper aligns the launcher.db "
                    "`project_kg_bindings` AND `kg_collection_access` rows "
                    "with that on-disk casing.\n\n"
                    "install.py v0.2.40 (2026-05-30) added a second pass: "
                    "when a binding row's `collection_name` is genuinely "
                    "missing AND has no case-sibling, probe for "
                    "`*_KnowledgeGraph` / `*_Development` classes with "
                    "non-zero row count; auto-adopt when exactly one matches "
                    "(typical post-`v0.2.29-cleanup` shape where the user "
                    "rebound the PRIMARY binding to a custom prefix like "
                    "`VCODev_*` but left the SHARED binding pointing at the "
                    "release-default canonical name)."
                ),
                command_to_apply=(
                    "No action required — the heal already ran. If you want "
                    "to verify the rebound rows, open the launcher and check "
                    "the Shared KG collection name on each affected project's "
                    "Settings → Identity tab."
                ),
                severity="info",
                kg_node_refs=[],
            )
        )

    if prefix_multi_candidates:
        # User intent is genuinely ambiguous — surface the candidates with
        # row counts and the explicit SQL to pick one.
        multi_lines = []
        for (pid, role, old_name, cands) in prefix_multi_candidates:
            cand_listing = "\n".join(
                f"      - `{name}` ({cnt} object(s))"
                for (name, cnt) in cands
            )
            multi_lines.append(
                f"  * project_id={pid} role={role}: advertised "
                f"`{old_name}` is missing; candidates with rows:\n"
                f"{cand_listing}"
            )
        multi_block = "\n".join(multi_lines)
        # Build a copy-paste SQL stanza per candidate-row for the user.
        sql_lines: list[str] = []
        for (pid, role, _old, cands) in prefix_multi_candidates:
            for (cand_name, _cnt) in cands:
                sql_lines.append(
                    f"UPDATE project_kg_bindings SET collection_name = "
                    f"'{cand_name}', config_json = "
                    f"'{{\"manual_override\":\"v0.2.40-prefix-adopt\"}}', "
                    f"updated_at = strftime('%s','now') * 1000 "
                    f"WHERE project_id = '{pid}' AND role = '{role}';"
                )
        sql_block = "\n".join(sql_lines)
        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="multi_candidate_prefix_adopt",
                title=(
                    f"Multiple populated KG collections match advertised "
                    f"`collection_name` suffix — manual choice required "
                    f"({len(prefix_multi_candidates)} row(s) ambiguous)"
                ),
                detected=(
                    f"For the following `project_kg_bindings` row(s), the "
                    f"advertised `collection_name` does not exist in "
                    f"Weaviate AND more than one populated class shares "
                    f"the suffix (`_KnowledgeGraph` / `_Development`). "
                    f"The cross-prefix self-heal refuses to guess.\n\n"
                    f"{multi_block}"
                ),
                why_deferred=(
                    "Auto-adoption with multiple non-empty candidates "
                    "would risk pointing the binding at the wrong data. "
                    "Pick the collection that matches your intent and "
                    "apply the SQL below directly against launcher.db "
                    "(see `command_to_apply`). If neither matches, "
                    "rename one in Weaviate first (out of scope for "
                    "install.py)."
                ),
                command_to_apply=(
                    f"# Pick ONE of the following lines per "
                    f"(project_id, role) tuple and run it against your "
                    f"launcher.db (default: ~/.vct/launcher.db).\n"
                    f"# Then re-run `python install.py --update` to "
                    f"propagate the new binding into .claude/env / "
                    f".claude/settings.json on next launcher boot.\n"
                    f"{sql_block}"
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )

    log_event(
        "7e/10", "ok",
        (
            f"self-healed {binding_count} case-binding(s) + "
            f"{access_count} access row(s) + "
            f"{prefix_adopt_count} prefix-adopt(s); "
            f"{len(prefix_multi_candidates)} ambiguous row(s) deferred"
        ),
        data={
            "rebinds": [
                {"project_id": pid, "role": role,
                 "old_collection_name": old,
                 "new_collection_name": new}
                for (pid, role, old, new) in rebinds
            ],
            "access_rebinds": [
                {"project_id": pid,
                 "old_collection_name": old,
                 "new_collection_name": new}
                for (pid, old, new) in access_rebinds
            ],
            "prefix_adopts": [
                {"project_id": pid, "role": role,
                 "old_collection_name": old,
                 "new_collection_name": new,
                 "adopted_row_count": cnt}
                for (pid, role, old, new, cnt) in prefix_adopts
            ],
            "multi_candidates": [
                {"project_id": pid, "role": role,
                 "old_collection_name": old,
                 "candidates": [
                     {"name": name, "row_count": cnt}
                     for (name, cnt) in cands
                 ]}
                for (pid, role, old, cands) in prefix_multi_candidates
            ],
        },
    )
