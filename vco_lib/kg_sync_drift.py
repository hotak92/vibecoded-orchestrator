# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Detect KG nodes that exist on disk but never reached Weaviate (v0.2.92).

Why this module exists
-----------------------
The canonical KG write path is "write ``knowledge/**/*.md``, and a
``PostToolUse`` hook syncs it to Weaviate". When that sync does not happen —
the hook didn't fire, the file was written outside a Claude Code session, the
sync errored, Weaviate was down, or the process was killed mid-run — there is
NO record that a sync was owed. The markdown looks fine on disk, so the node
is silently invisible to every future ``hybrid_search`` / ``kg-search`` call,
and the failure presents as a valid EMPTY answer rather than a loud error.

This module closes that gap with **detection, not repair**: it compares the
on-disk ``knowledge/`` tree against what a project's KG collection (and, for
``scope: shared`` nodes, the shared collection) actually holds, by content
hash — never by trusting that "the file exists" implies "the sync ran".

Deliberately out of scope (see :mod:`templates.scripts.sync_knowledge_graph`
and the work-package brief this module was written under): no automatic bulk
re-sync. A drift finding is SURFACED (deferral ledger entry with a command
the user/agent can run) and returned to the caller; nothing here ever writes
to Weaviate.

Two DISTINCT failures, not one (2026-09-01 addendum)
-----------------------------------------------------
A field report from a module that creates VCO projects programmatically
sharpened the requirement this module was built to close. Their writers are
Claude Code sessions with file tools only, relying on the PostToolUse hook to
re-index what they write — but a project that is REGISTERED with the
orchestrator/hub yet never had its bundle installed has no ``.claude/``
tree at all: no ``settings.json``, no hook, no ``.claude/scripts/kg-sync``,
no ``.claude/scripts/kg-search``. The writer still succeeds (writing a file
always succeeds), the envelope says "complete", and NO component anywhere
reports an error — the symptom is "it says it wrote 40 nodes, search returns
nothing", every individual signal green.

:func:`scan_drift` answers "binding exists — are some NODES unsynced?".
:func:`check_kg_binding` answers a cheaper, PRIOR question: "does this
project have a KG collection binding AT ALL?" — a project with content under
``knowledge/`` and no resolvable binding is a SETUP GAP, not drift; no
per-node hash comparison could ever detect it (there is no collection to
compare against), which is exactly why it needs its own check rather than
folding into :func:`scan_drift`'s empty-result path.

Both functions take their Weaviate/collection parameters as plain arguments
— never reading them from a target project's own ``.claude/`` bundle state —
specifically so they remain callable in the exact scenario where they are
most needed: a project with NO bundle on disk. :func:`main` (``python -m
vco_lib.kg_sync_drift``) is the bundle-independent entry point; the
``--check-drift`` subcommand wired into ``sync_knowledge_graph.py`` is a
SECOND, bundle-DEPENDENT entry point for the common bundled-project case and
is not reachable in the unbundled scenario — see this module's own report
for that honestly-stated design limit: closing it for real requires wiring a
caller that runs independently of any per-project bundle (the module system
that writes these projects, or an orchestrator-level periodic scan), which
is outside this work package's file boundary.

One-home reuse (this module invents nothing new):

* Content hashing — :func:`vco_lib.knowledge_residue.content_signature_excluding_updated`,
  the SAME storage-layer signature ``sync_knowledge_graph.py`` computes when it
  actually syncs a node (parity-locked with that script; see the module's own
  docstring on why a mirrored implementation is used instead of an import —
  the sync script has heavy module-level side effects on import). A past
  release (v0.2.75) was bitten by a sidecar hash-scheme MISMATCH; reusing this
  function instead of inventing a third hash scheme is the fix for that class
  of bug, not just this one instance.
* Fetching what Weaviate actually has — :func:`vco_lib.kg_sync.batch_query_content_hashes`,
  the SAME hash-diff GraphQL query ``install.py``'s CI-10 gate and the v0.2.46
  KG-rebind path already use (the V46-A safety triad: no ``Like "%"`` filter,
  ``limit: 10000``, inspect ``errors[]`` before ``data``).
* Reachability probing — :func:`vco_lib.knowledge_residue.weaviate_reachable`.
* Surfacing — :mod:`vco_lib.deferral_emit` (the ONE emitter home for
  ``UPDATE_DEFERRED.{md,json}``; see that module's docstring), the same
  channel ``kg_sync_no_embedding_backend`` and ``residue_cleanup_pending``
  already use. The condition this module emits, ``kg_sync_drift_detected``,
  is classified ``disposition="action_required"`` EXPLICITLY on the entry
  (rather than left to register in ``vco_lib/deferral_conditions.toml``,
  which is outside this work package's file boundary) — see
  :func:`surface_drift` for why an unregistered condition must never claim
  ``auto_retryable`` it cannot back up with a real dispatcher handler.

Exclusions this module MUST mirror (parity-tested against the source file —
see ``tests/test_v0292_kg_sync_drift.py``):

* ``TAG_HIERARCHY.md`` / ``VOCABULARY.md`` are never synced at all (schema/
  reference docs, not searchable content) — MUST MATCH
  ``sync_knowledge_graph.py::sync_all_nodes``'s ``EXCLUDED_FILES``.
* Archived nodes (path segment ``archive`` / ``.archive`` / ``_archive``, or
  frontmatter ``status: archived|deprecated|superseded``) are deliberately
  absent from Weaviate — MUST MATCH ``sync_knowledge_graph.py::_is_archived_node``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml

from vco_lib.knowledge_residue import (
    content_signature_excluding_updated,
    weaviate_reachable,
)
from vco_lib.kg_sync import batch_query_content_hashes

#: MUST MATCH sync_knowledge_graph.py::sync_all_nodes's EXCLUDED_FILES —
#: schema/reference docs that are never synced, checked by basename only.
EXCLUDED_SYNC_BASENAMES: frozenset = frozenset({"TAG_HIERARCHY.md", "VOCABULARY.md"})

#: MUST MATCH sync_knowledge_graph.py::_is_archived_node's
#: _ARCHIVE_DIR_SEGMENTS — exact path-segment match, never substring (so
#: `architecture/` and `archived-notes/` are NOT caught).
ARCHIVE_DIR_SEGMENTS: frozenset = frozenset({"archive", ".archive", "_archive"})

#: MUST MATCH sync_knowledge_graph.py::_is_archived_node's status check.
ARCHIVED_STATUS_VALUES: frozenset = frozenset({"archived", "deprecated", "superseded"})

#: Deferral condition ids this module owns.
CID_DRIFT = "kg_sync_drift_detected"
#: A project has content under knowledge/ but no resolvable KG collection
#: binding at all (the "registered but unbundled" setup gap — distinct from
#: drift, which requires a binding to exist in the first place).
CID_UNBOUND = "kg_binding_missing"


# ---------------------------------------------------------------------------
# Small self-contained mirrors of sync_knowledge_graph.py decisions
#
# (C-leg of the CLAUDE.md A>B>C cross-language/cross-module rule: importing
# sync_knowledge_graph.py directly is not viable here — that module resolves
# the hub, filters warnings, and imports `weaviate` as module-level side
# effects, none of which a read-only drift scan should trigger. The mirrored
# logic is intentionally tiny and pinned by a source-scan parity test.)
# ---------------------------------------------------------------------------

def is_archived_path(rel_parts: tuple) -> bool:
    """Path-segment leg of the archived check. Exact segment match only."""
    return any(p in ARCHIVE_DIR_SEGMENTS for p in rel_parts)


def _frontmatter_field(content: str, key: str) -> Optional[str]:
    """Best-effort extraction of one top-level frontmatter string field.

    Returns None on missing frontmatter, a missing key, unparseable YAML, or
    a non-string value — every case defaults to "cannot tell", which is the
    safe direction for both callers (``status`` absent ⇒ not archived by
    status; ``scope`` absent ⇒ "project", matching ``_node_scope``'s own
    default).
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
    val = fm.get(key)
    return val if isinstance(val, str) else None


def is_archived_node(rel_parts: tuple, content: str) -> bool:
    """Full archived check (path OR frontmatter status) — mirrors
    ``_is_archived_node`` (reason string dropped; callers only need bool)."""
    if is_archived_path(rel_parts):
        return True
    status = _frontmatter_field(content, "status")
    if status is not None and status.strip().lower() in ARCHIVED_STATUS_VALUES:
        return True
    return False


def node_scope(content: str) -> str:
    """Mirrors ``_node_scope``: 'shared' only for an exact, valid value;
    everything else (absent, invalid) resolves to 'project'."""
    raw = _frontmatter_field(content, "scope")
    if raw is not None and raw.strip().lower() == "shared":
        return "shared"
    return "project"


def _row_path_shapes(rel_posix: str) -> tuple:
    """Both stored ``file_path`` shapes for a ``knowledge/`` rel path.

    Reuses ``knowledge_residue``'s helper (same convention: rows written on
    POSIX carry forward slashes, rows written on Windows carry backslashes —
    the v0.2.81 separator lesson) rather than re-deriving it.
    """
    from vco_lib.knowledge_residue import _row_path_shapes as _rps

    return tuple(_rps(rel_posix))


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DriftReport:
    """Result of one drift scan. JSON-serialisable via ``__dict__``-shaped use."""

    #: "ok" (no drift, or nothing to check) · "drift" (missing/stale nodes
    #: found) · "unknown" (could not reliably determine — e.g. Weaviate
    #: unreachable; NEVER conflated with "drift").
    status: str
    scanned: int = 0
    archived_skipped: int = 0
    excluded_skipped: int = 0
    shared_scope_skipped: int = 0
    missing: tuple = field(default_factory=tuple)
    stale: tuple = field(default_factory=tuple)
    detail: str = ""

    @property
    def drifted(self) -> tuple:
        """All drifted rel paths (missing + stale), sorted."""
        return tuple(sorted((*self.missing, *self.stale)))


def _collect_candidate_nodes(knowledge_root: Path) -> "tuple[list, int]":
    """Return ``(nodes, excluded_count)``.

    ``nodes`` holds ``(rel_posix, rel_parts, content)`` for every markdown
    file under *knowledge_root* that is NOT excluded-by-basename and is
    readable. ``excluded_count`` is how many matched
    :data:`EXCLUDED_SYNC_BASENAMES` (schema/reference docs that
    ``sync_knowledge_graph.py`` never syncs at all — reported separately so
    the caller can distinguish "never meant to be in Weaviate" from every
    other bucket). Archived-ness and scope are resolved by the caller (they
    need the content, which is already read here — avoid a second file read
    per node).
    """
    if not knowledge_root.is_dir():
        return [], 0
    nodes: list = []
    excluded = 0
    for f in sorted(knowledge_root.rglob("*.md")):
        if not f.is_file():
            continue
        if f.name in EXCLUDED_SYNC_BASENAMES:
            excluded += 1
            continue
        try:
            rel_parts = f.relative_to(knowledge_root).parts
        except ValueError:  # pragma: no cover — rglob guarantees containment
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # unreadable ⇒ cannot check either way; skip silently
        rel_posix = "knowledge/" + "/".join(rel_parts)
        nodes.append((rel_posix, rel_parts, content))
    return nodes, excluded


def scan_drift(
    knowledge_root: Path,
    *,
    weaviate_url: str,
    kg_collection: str,
    shared_kg_collection: str = "",
    reachable_fn: Callable[[str], bool] = weaviate_reachable,
    query_hashes_fn: Callable[..., dict] = batch_query_content_hashes,
) -> DriftReport:
    """Compare on-disk ``knowledge/`` content hashes against Weaviate.

    Args:
        knowledge_root: the project's ``knowledge/`` directory.
        weaviate_url: base URL, e.g. ``http://localhost:8081``.
        kg_collection: the project's own KG collection name.
        shared_kg_collection: the shared-KG collection name, or ``""`` when
            not configured. ``scope: shared`` nodes are checked against this
            collection instead of ``kg_collection`` (their rows are migrated
            OUT of the project collection by ``_finish_shared_scope_write``).
            A ``scope: shared`` node is counted in ``shared_scope_skipped``
            (never flagged as drifted) when no shared collection is
            configured — checking the wrong collection would produce a wrong
            answer, and "cannot tell" must never render as "missing".
        reachable_fn: injectable Weaviate-liveness probe (tests replace this).
        query_hashes_fn: injectable hash-fetch (tests replace this).

    Returns:
        A :class:`DriftReport`. ``status="unknown"`` whenever the comparison
        could not be trusted — Weaviate unreachable, or the hash-fetch itself
        reported a transport/GraphQL failure — NEVER rendered as "every node
        is missing". Also ``"unknown"`` (never "drift") when *kg_collection*
        is empty: a missing binding is a DIFFERENT question, answered by
        :func:`check_kg_binding` — querying Weaviate for an empty collection
        name would risk an incidental GraphQL response being misread as "0
        stored nodes" (⇒ false "everything is missing") rather than the
        honest "there is nothing configured to compare against".
    """
    knowledge_root = Path(knowledge_root)

    if not kg_collection or not str(kg_collection).strip():
        return DriftReport(
            status="unknown",
            detail=(
                "no kg_collection configured — cannot determine drift "
                "(see check_kg_binding for the distinct 'no binding at "
                "all' state)"
            ),
        )

    candidates, excluded_skipped = _collect_candidate_nodes(knowledge_root)

    if not candidates:
        # Empty (or absent) knowledge/ ⇒ trivially nothing to report. No
        # Weaviate round-trip needed — this is also what keeps a fresh
        # project (no KG yet) from ever probing a backend just to learn
        # "there is nothing to check".
        return DriftReport(
            status="ok",
            scanned=0,
            excluded_skipped=excluded_skipped,
            detail="no knowledge/ nodes on disk",
        )

    # Bucket candidates BEFORE touching the network — cheap, deterministic,
    # and lets us report "0 checkable nodes" honestly even with Weaviate down.
    project_nodes: list = []      # (rel_posix, computed_hash)
    shared_nodes: list = []       # (rel_posix, computed_hash)
    archived_skipped = 0
    shared_scope_skipped = 0

    for rel_posix, rel_parts, content in candidates:
        if is_archived_node(rel_parts, content):
            archived_skipped += 1
            continue
        computed_hash = content_signature_excluding_updated(content)
        if node_scope(content) == "shared":
            if shared_kg_collection:
                shared_nodes.append((rel_posix, computed_hash))
            else:
                shared_scope_skipped += 1
            continue
        project_nodes.append((rel_posix, computed_hash))

    checkable = project_nodes + shared_nodes
    if not checkable:
        return DriftReport(
            status="ok",
            scanned=len(candidates),
            archived_skipped=archived_skipped,
            excluded_skipped=excluded_skipped,
            shared_scope_skipped=shared_scope_skipped,
            detail="no non-archived, checkable nodes on disk",
        )

    if not reachable_fn(weaviate_url):
        return DriftReport(
            status="unknown",
            scanned=len(candidates),
            archived_skipped=archived_skipped,
            excluded_skipped=excluded_skipped,
            shared_scope_skipped=shared_scope_skipped,
            detail=f"Weaviate unreachable at {weaviate_url!r} — drift could not be determined",
        )

    # Track transport/GraphQL problems separately per collection so a failure
    # fetching ONE (e.g. an unconfigured shared collection) does not discard
    # a perfectly good answer for the other.
    warnings_seen: list = []

    def _on_warn(kind: str, info: dict) -> None:
        warnings_seen.append((kind, info))

    project_hashes = (
        query_hashes_fn(weaviate_url, kg_collection, on_warn=_on_warn)
        if project_nodes
        else {}
    )
    project_query_failed = project_nodes and not project_hashes and any(
        k in ("transport_failure", "graphql_errors") for k, _ in warnings_seen
    )

    shared_hashes: dict = {}
    shared_query_failed = False
    if shared_nodes:
        pre_len = len(warnings_seen)
        shared_hashes = query_hashes_fn(weaviate_url, shared_kg_collection, on_warn=_on_warn)
        shared_query_failed = not shared_hashes and any(
            k in ("transport_failure", "graphql_errors") for k, _ in warnings_seen[pre_len:]
        )

    if project_query_failed or shared_query_failed:
        return DriftReport(
            status="unknown",
            scanned=len(candidates),
            archived_skipped=archived_skipped,
            excluded_skipped=excluded_skipped,
            shared_scope_skipped=shared_scope_skipped,
            detail="the Weaviate hash query failed — drift could not be determined",
        )

    missing: list = []
    stale: list = []
    for rel_posix, computed_hash, hashes in (
        *((r, h, project_hashes) for r, h in project_nodes),
        *((r, h, shared_hashes) for r, h in shared_nodes),
    ):
        stored_hash = None
        # _row_path_shapes takes a path RELATIVE TO knowledge/ (no prefix)
        # and returns both fully-prefixed stored-row shapes
        # ("knowledge/x/y.md" and, on a Windows-written row, "knowledge\x\y.md").
        for shape in _row_path_shapes(rel_posix[len("knowledge/"):]):
            if shape in hashes:
                stored_hash = hashes[shape]
                break
        if stored_hash is None:
            missing.append(rel_posix)
        elif stored_hash != computed_hash:
            stale.append(rel_posix)

    status = "drift" if (missing or stale) else "ok"
    detail = (
        f"{len(missing)} missing, {len(stale)} stale out of {len(checkable)} checked"
        if status == "drift"
        else f"{len(checkable)} node(s) verified in sync"
    )
    return DriftReport(
        status=status,
        scanned=len(candidates),
        archived_skipped=archived_skipped,
        excluded_skipped=excluded_skipped,
        shared_scope_skipped=shared_scope_skipped,
        missing=tuple(sorted(missing)),
        stale=tuple(sorted(stale)),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# The setup-gap check — a DIFFERENT question from scan_drift's
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BindingCheck:
    """Result of :func:`check_kg_binding`."""

    #: "bound" (a KG collection name was resolved somewhere) · "unbound"
    #: (knowledge/ has content but no collection name could be found
    #: anywhere) · "ok" (nothing to bind — no non-archived content).
    status: str
    kg_collection: str = ""
    detail: str = ""


def _local_kg_collection_hint(folder: Path) -> str:
    """Best-effort LOCAL (no network) resolution of ``KG_COLLECTION``.

    Reads ``.claude/settings.json`` ``env`` then ``.claude/env`` — same two
    files and the same precedence ``knowledge_residue.shared_read_disabled_for``
    already uses for its own gate lookup. Empty string when the project has
    no ``.claude/`` bundle at all (the exact "registered but unbundled"
    case) or neither file carries the key. Never raises.
    """
    from vco_lib.knowledge_residue import project_settings_env

    env = project_settings_env(folder)
    val = env.get("KG_COLLECTION")
    if isinstance(val, str) and val.strip():
        return val.strip()
    try:
        env_file = Path(folder) / ".claude" / "env"
        if env_file.is_file():
            text = env_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(
                r'^\s*(?:export\s+)?KG_COLLECTION=["\']?([^"\'\s]+)',
                text,
                flags=re.MULTILINE,
            )
            if m:
                return m.group(1).strip()
    except Exception:  # noqa: BLE001 — local config read is best-effort
        pass
    return ""


def check_kg_binding(
    folder: Path,
    knowledge_root: Path,
    *,
    kg_collection: str = "",
    resolve_hub: bool = True,
    resolve_cfg_fn: Optional[Callable[[Path], object]] = None,
) -> BindingCheck:
    """Does *folder* have a resolvable KG collection binding AT ALL?

    This answers a PRIOR, cheaper question than :func:`scan_drift`: not "are
    some nodes unsynced" but "is there even a collection to sync them to".
    A project can have Claude Code sessions successfully writing
    ``knowledge/*.md`` files all day — each write's own envelope reports
    success, because writing a file always succeeds — while NO sync
    mechanism exists to notice, because a project that is registered with
    the orchestrator/hub but never had its bundle installed has no
    ``.claude/`` tree at all: no hook, no ``.claude/scripts/kg-sync``, no
    ``.claude/scripts/kg-search``. Every individual signal is healthy; the
    aggregate ("did this content ever become searchable") is not, and no
    per-node hash comparison could ever surface it — there is no collection
    to compare against. This is a distinct, cheaper, no-Weaviate-round-trip
    check for exactly that state.

    Args:
        folder: the project root to check (used for local config reads and
            the hub/project_config resolution — NOT written to).
        knowledge_root: the project's ``knowledge/`` directory (used only to
            decide whether there is any non-archived content worth binding
            at all — an empty tree makes the binding question moot).
        kg_collection: an explicit collection name, when the CALLER already
            knows it (e.g. resolved via its own registry/hub call, exactly
            the shape a module system managing project registration would
            have on hand). Passing this bypasses file-based resolution
            entirely — the property that makes this check usable for a
            project with NO bundle on disk: nothing here needs
            ``.claude/scripts/*`` to exist.
        resolve_hub: attempt ``vco_lib.project_config.resolve(folder)``
            (the same hub-then-env resolver ``sync_knowledge_graph.py``
            uses) when no explicit *kg_collection* was given and no local
            config resolves one. Set False to skip network entirely (pure
            local + explicit resolution only).
        resolve_cfg_fn: injectable replacement for
            ``vco_lib.project_config.resolve`` (tests use this instead of a
            real hub round-trip).

    Returns:
        A :class:`BindingCheck`. Unlike :func:`scan_drift` there is no
        "unknown" status here: resolving a collection NAME is a pure
        local/config operation (a hub probe failure is treated the same as
        "hub has nothing to say" — falls through to the next resolution
        leg, same fail-open contract every other hub-resolver caller in
        this codebase already uses), so the answer is always a definite
        bound/unbound/ok.
    """
    folder = Path(folder)
    knowledge_root = Path(knowledge_root)

    candidates, _excluded = _collect_candidate_nodes(knowledge_root)
    has_content = any(
        not is_archived_node(rel_parts, content)
        for _rel, rel_parts, content in candidates
    )
    if not has_content:
        return BindingCheck(
            status="ok", detail="no non-archived knowledge/ content to bind",
        )

    resolved = kg_collection.strip() if kg_collection else ""
    source = "explicit argument" if resolved else ""

    if not resolved and resolve_hub:
        try:
            resolver = resolve_cfg_fn or _default_resolve_project_config
            cfg = resolver(folder)
            candidate = (getattr(cfg, "kg_collection", "") or "").strip()
            if candidate:
                resolved = candidate
                source = "hub/project_config"
        except Exception:  # noqa: BLE001 — fail-open, same contract as
            pass          # every other hub-resolver caller in this codebase

    if not resolved:
        local = _local_kg_collection_hint(folder)
        if local:
            resolved = local
            source = "local .claude config"

    if resolved:
        return BindingCheck(
            status="bound", kg_collection=resolved,
            detail=f"resolved via {source}",
        )

    return BindingCheck(
        status="unbound",
        detail=(
            "knowledge/ has non-archived content but no KG collection "
            "binding could be resolved anywhere (explicit argument, "
            "hub/project_config, or local .claude config) — this project "
            "may be registered but never bundled: writes to knowledge/ "
            "succeed and nothing errors, but nothing syncs either"
        ),
    )


def _default_resolve_project_config(folder: Path):
    """Lazy import wrapper so a broken/absent project_config module cannot
    break the import of this whole module — only this ONE resolution leg."""
    from vco_lib.project_config import resolve

    return resolve(folder)


# ---------------------------------------------------------------------------
# Surfacing (deferral ledger — detection only, never an unattended re-sync)
# ---------------------------------------------------------------------------

def surface_drift(
    folder: Path,
    report: DriftReport,
    *,
    log: object = None,
) -> None:
    """Reflect *report* into the project's deferral ledger. Never raises.

    * ``status == "drift"`` → emit (or refresh) ``kg_sync_drift_detected``,
      ``disposition="action_required"`` EXPLICITLY. This module cannot
      register a real ``auto_retryable`` handler (that requires editing
      ``vco_lib/deferral_conditions.toml`` + ``vco_lib/deferral_retry.py``,
      outside this work package's file boundary) — claiming ``auto_retryable``
      without a wired handler would tell the reader "this resolves itself"
      when nothing retries it, which is a worse lie than the gap this module
      exists to close. ``command_to_apply`` prints the SAFE reconciliation
      command (a normal ``--all`` sync — content-hash gated, never deletes
      anything) rather than any destructive action.
    * ``status == "ok"`` → resolve any previously-emitted entry (paired
      clear — mirrors ``sync_knowledge_graph.py``'s own
      ``_clear_sync_deferral_no_backend`` pattern for its sibling condition).
    * ``status == "unknown"`` → no-op. An inconclusive probe must never
      overwrite a previously-recorded finding in either direction.
    """
    folder = Path(folder)
    if report.status == "ok":
        try:
            from vco_lib.deferral_emit import resolve_conditions

            resolve_conditions(folder, (CID_DRIFT,))
        except Exception:  # noqa: BLE001 — ledger bookkeeping is best-effort
            pass
        return

    if report.status != "drift":
        return  # "unknown" — say nothing rather than guess

    try:
        from vco_lib.deferral_emit import emit
        from vco_lib.deferral_report import DeferralEntry

        shown = report.drifted[:10]
        lines = "\n".join(f"- `{p}`" for p in shown)
        if len(report.drifted) > len(shown):
            lines += f"\n- … and {len(report.drifted) - len(shown)} more"
        entry = DeferralEntry(
            condition_id=CID_DRIFT,
            title="Knowledge-graph nodes on disk are not reflected in Weaviate",
            detected=(
                f"{len(report.missing)} node(s) exist under `knowledge/` with no "
                f"matching row in Weaviate, and {len(report.stale)} have a "
                f"content hash that no longer matches the stored row (a sync "
                f"was owed but never completed, or completed against stale "
                f"content):\n{lines}"
            ),
            why_deferred=(
                "Detection only, by design — this scan never writes to "
                "Weaviate on its own. A node's markdown looks fine on disk, "
                "so this drift is otherwise invisible: hybrid_search / "
                "kg-search silently return without it, and the gap presents "
                "as a valid empty answer rather than an error."
            ),
            command_to_apply=(
                "# Re-run the sync for the affected project (content-hash "
                "gated — unaffected nodes are skipped, nothing is deleted):\n"
                ".claude/scripts/kg-sync --all"
            ),
            severity="warning",
            disposition="action_required",
        )
        emit(folder, entry, log=log)
    except Exception:  # noqa: BLE001 — surfacing must never crash the caller
        pass


def surface_binding_gap(
    folder: Path,
    check: BindingCheck,
    *,
    log: object = None,
) -> None:
    """Reflect *check* into the project's deferral ledger. Never raises.

    Same shape as :func:`surface_drift` but for the DISTINCT setup-gap
    condition (:data:`CID_UNBOUND`) — kept as a separate condition id
    deliberately: "no binding at all" and "binding exists, some nodes
    unsynced" are different failures with different remediations (register
    + bundle the project, vs. re-run a sync), and collapsing them into one
    entry would tell the reader the wrong thing to do.

    * ``status == "unbound"`` → emit (or refresh) the entry,
      ``disposition="action_required"`` (same reasoning as
      :func:`surface_drift`: no real ``auto_retryable`` handler is wired for
      this condition either).
    * ``status == "bound"`` or ``"ok"`` → resolve any previously-emitted
      entry (paired clear).
    """
    folder = Path(folder)
    if check.status != "unbound":
        try:
            from vco_lib.deferral_emit import resolve_conditions

            resolve_conditions(folder, (CID_UNBOUND,))
        except Exception:  # noqa: BLE001 — ledger bookkeeping is best-effort
            pass
        return

    try:
        from vco_lib.deferral_emit import emit
        from vco_lib.deferral_report import DeferralEntry

        entry = DeferralEntry(
            condition_id=CID_UNBOUND,
            title="knowledge/ has content but no KG collection binding was found",
            detected=check.detail,
            why_deferred=(
                "Detection only. Resolving this requires knowing which "
                "collection this project SHOULD bind to — a decision this "
                "check cannot make on the caller's behalf (it would be "
                "guessing a destination for content that has never been "
                "synced anywhere)."
            ),
            command_to_apply=(
                "# Register/bundle this project so a KG collection binding "
                "exists, then run a full sync:\n"
                "python install.py --update   # or your module system's "
                "equivalent bundle-install step\n"
                ".claude/scripts/kg-sync --all"
            ),
            severity="warning",
            disposition="action_required",
        )
        emit(folder, entry, log=log)
    except Exception:  # noqa: BLE001 — surfacing must never crash the caller
        pass


# ---------------------------------------------------------------------------
# Standalone CLI — deliberately independent of any per-project bundle.
#
# `sync_knowledge_graph.py --check-drift` (wired in the same v0.2.92 change)
# is a SECOND, bundle-DEPENDENT entry point for the common case where a
# project already has `.claude/scripts/` installed. This one does not need
# it: only `vco_lib` itself needs to be importable, which is true for the
# orchestrator's own tooling and for any module system that creates VCO
# projects programmatically (the exact caller the "registered but
# unbundled" scenario needs). It never resolves collection names from a
# target project's OWN bundle state — everything is either passed
# explicitly or resolved via the hub/local-config legs `check_kg_binding`
# already defines.
# ---------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    """``python -m vco_lib.kg_sync_drift --project-root DIR [options]``.

    Runs :func:`check_kg_binding` first (cheap, no Weaviate round-trip); if
    unbound, reports that and stops — there is nothing for :func:`scan_drift`
    to compare against. Otherwise runs :func:`scan_drift` and reports its
    result. Both findings are surfaced to the target project's deferral
    ledger unless ``--no-surface`` is given. Always exits 0 when the CHECK
    itself completed (including "unbound", "drift", and "unknown" outcomes)
    — a non-zero exit would wrongly signal "this run failed" to an
    automated caller; exits 2 only for a genuine usage error.
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.kg_sync_drift",
        description=(
            "Detect (never repair) knowledge/ content that never reached "
            "Weaviate — both 'no binding at all' and 'binding exists, some "
            "nodes unsynced'. Bundle-independent: works for a project with "
            "no .claude/ tree, as long as --kg-collection (or a resolvable "
            "hub/local binding) is available."
        ),
    )
    parser.add_argument("--project-root", type=Path, required=True,
                        help="project folder containing knowledge/")
    parser.add_argument("--weaviate-url", default="http://localhost:8081")
    parser.add_argument("--kg-collection", default="",
                        help="explicit KG collection name (bypasses hub/local resolution)")
    parser.add_argument("--shared-kg-collection", default="")
    parser.add_argument("--no-hub", action="store_true",
                        help="skip the hub/project_config resolution leg (local-only)")
    parser.add_argument("--no-surface", action="store_true",
                        help="print findings but do not write to the deferral ledger")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    folder = Path(args.project_root)
    knowledge_root = folder / "knowledge"

    binding = check_kg_binding(
        folder, knowledge_root,
        kg_collection=args.kg_collection,
        resolve_hub=not args.no_hub,
    )
    if not args.no_surface:
        surface_binding_gap(folder, binding)

    result: dict = {"binding": {
        "status": binding.status,
        "kg_collection": binding.kg_collection,
        "detail": binding.detail,
    }}

    drift_report: Optional[DriftReport] = None
    if binding.status == "bound":
        drift_report = scan_drift(
            knowledge_root,
            weaviate_url=args.weaviate_url,
            kg_collection=binding.kg_collection or args.kg_collection,
            shared_kg_collection=args.shared_kg_collection,
        )
        if not args.no_surface:
            surface_drift(folder, drift_report)
        result["drift"] = {
            "status": drift_report.status,
            "scanned": drift_report.scanned,
            "missing": list(drift_report.missing),
            "stale": list(drift_report.stale),
            "detail": drift_report.detail,
        }

    if args.json:
        print(_json.dumps(result))
        return 0

    print(f"binding: {binding.status} — {binding.detail}")
    if drift_report is not None:
        print(f"drift:   {drift_report.status} — {drift_report.detail}")
        for p in drift_report.missing:
            print(f"  missing: {p}")
        for p in drift_report.stale:
            print(f"  stale:   {p}")
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI entry
    import sys as _sys

    _sys.exit(main())


__all__ = [
    "ARCHIVE_DIR_SEGMENTS",
    "ARCHIVED_STATUS_VALUES",
    "CID_DRIFT",
    "CID_UNBOUND",
    "EXCLUDED_SYNC_BASENAMES",
    "BindingCheck",
    "DriftReport",
    "check_kg_binding",
    "is_archived_node",
    "is_archived_path",
    "main",
    "node_scope",
    "scan_drift",
    "surface_binding_gap",
    "surface_drift",
]
