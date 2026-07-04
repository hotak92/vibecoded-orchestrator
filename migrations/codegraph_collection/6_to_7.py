# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# @idempotent: yes
# @destructive: no
# @classification: derived
"""codegraph_collection content edge v6 → v7 (v0.2.73 READ-amp origin cleanup).

ONE-TIME targeted purge of transient-scratch rows that never should have been
indexed. It deletes ONLY the objects whose ``file_path`` lives under
``.claude/state/`` (``tool_backups/`` snapshot copies + per-session scratch),
preserving EVERY real object's vector AND its generated summary — NO drop, NO
re-embed, NO re-summarize.

Why this edge exists
--------------------
On an orchestrator clone (``index_dot_claude=True``) — and on any project whose
codegraph walks the orchestrator via an ``--extra-path`` — the pre-v0.2.73
directory walk descended into ``.claude/`` and reached
``.claude/state/tool_backups/``. Because ``state`` was in no ignore set, every
timestamped backup ``.py``/``.rs``/… snapshot was indexed as a real function.
Live-measured 2026-07-04: **16,143 such rows = 43% of a real project's
CodeFunction collection**, and re-indexing them across sessions inflated the
collection to 120 GB / 333 LSM segments (live data a few MB), which drove a
multi-TB mmap READ storm. The code fix (``analyze_code_graph._path_is_excluded``
+ per-root ``.claude`` gate, commit 1e2fe1bb) stops NEW rows; this edge removes
the ALREADY-indexed garbage from existing installs on update, algorithmically —
no user action, no "ask Claude to run a script".

Why a delete (not a drop / not a --prune-stale reanalyze)
---------------------------------------------------------
  * A ``--force-recreate`` drop would discard every vector AND every LLM-
    generated summary → an expensive full re-embed + re-summarize. Rejected.
  * A ``--prune-stale`` reanalyze also cleans these rows, but requires a full
    walk + touches the whole collection. This edge is the SURGICAL subset: per
    class it enumerates rows, matches the ``.claude/state/`` marker on each raw
    ``file_path`` in Python, and ``delete_by_id``s ONLY those UUIDs — nothing
    else. (It deliberately does NOT use a Weaviate ``Like`` filter: ``file_path``
    is ``word``-tokenized, so a ``Like "*.claude/state/*"`` matches on the tokens
    ``claude``/``state`` — unsafe for a DELETE. See ``_TRANSIENT_MARKER``.)

Preserving contract: the survivors are untouched — their stored vectors and
``*_summary`` / ``*_description`` properties are never read or rewritten here.
This is why the edge is ``@destructive: no`` for a ``derived`` collection: it
removes provable NON-source garbage and self-heals the collection toward its
canonical content; nothing regenerable-only-at-cost is lost.

Idempotency: re-running finds zero ``.claude/state/**`` rows and deletes
nothing. Safe to repeat; the runner keys the v7 advance on exit 0.

Disk reclaim: deleting the rows tombstones their vectors; with the v0.2.73
2 GiB ``PERSISTENCE_LSM_MAX_SEGMENT_SIZE`` cap, the next compaction cycle
collapses the freed segments and reclaims the on-disk bloat. That reclaim is
Weaviate-side + asynchronous — this edge's job is only to stop the rows
existing; it does not (and must not) block the update on compaction.

Exit codes (the runner keys the version advance on exit 0):
  * 0 — the purge ran (any number deleted, including zero) OR the classes are
    absent OR no codegraph project is resolvable from env.
  * non-zero — Weaviate unreachable / a class enumerate error / one-or-more
    ``delete_by_id`` failures (a half-applied edge). The runner then does NOT
    advance the recorded version and re-attempts next
    update (the leftover rows are the pre-fix status quo, never a regression).
"""

from __future__ import annotations

import os
import re
import sys


#: The FILE-ANCHORED code classes and their per-file path property: Module keys
#: the file on ``path``; Function/Class on ``file_path`` (v0.2.52 V52-O.4).
#:
#: CodeAPI / CodeInteraction are DELIBERATELY EXCLUDED: they are not 1:1
#: file-anchored (they carry ``endpoint``/``handler``, NOT a file-path property —
#: a ``file_path`` filter 500s them with "no such prop"). This mirrors
#: ``analyze_code_graph._prune_deleted_file_objects``, which for the same reason
#: prunes only Module/Function/Class and lets a later full ``--prune-stale``
#: reanalyze reconcile any stray API/Interaction rows. The bulk of the
#: ``.claude/state`` garbage is functions/classes/modules anyway; API/Interaction
#: rows are not minted from ``tool_backups`` snapshot files.
_CLASS_PATH_PROP = {
    "CodeModule": "path",
    "CodeFunction": "file_path",
    "CodeClass": "file_path",
}

#: The transient-scratch marker: a real source row's repo-relative path NEVER
#: contains this substring (``.claude/state/`` is orchestrator scratch, never
#: source; a real ``.claude/hooks/`` or ``.claude/scripts/`` file does NOT match).
#:
#: WHY an EXACT PYTHON SUBSTRING and NOT a Weaviate ``Like`` filter (this is a
#: SAFETY-CRITICAL decision, live-verified 2026-07-04): ``file_path`` is a
#: ``text`` property with ``tokenization=word``. A ``Like "*.claude/state/*"``
#: therefore matches on the TOKENS ``claude`` + ``state`` (word-tokenized), NOT
#: the literal path substring — a subtle, version-dependent semantics that is
#: unsafe to trust for a DELETE. (Conversely, an ``Equal`` on this field is ALSO
#: token-set matching, not exact-string equality.) So we NEVER hand the marker to
#: Weaviate: we iterate rows, read each raw ``file_path`` back, and delete ONLY
#: the UUIDs whose Python string literally contains this marker. Dry-run on a real
#: 37,702-object collection: 16,143 matched (all under ``.claude/state/``), 21,559
#: kept (incl. real ``.claude/hooks/`` source) — zero real rows matched.
_TRANSIENT_MARKER = ".claude/state/"


def _resolve_codegraph_prefix() -> str | None:
    """Resolve the project's codegraph class prefix from env, mirroring
    ``schema_migration_runner._resolve_codegraph_prefix`` /
    ``analyze_code_graph._sanitize_collection_prefix`` (kept identical to
    ``5_to_6._resolve_codegraph_prefix`` — same resolution, do not diverge).

    ``CODE_GRAPH_PROJECT`` (already the sanitized prefix the analyzer uses)
    wins; else derive it from ``PROJECT_NAME`` via the SAME
    ``canonical_class_prefix`` helper + legacy regex fallback so we probe the
    class name the analyzer actually wrote. Returns ``None`` when neither is set.
    """
    cg = (os.environ.get("CODE_GRAPH_PROJECT") or "").strip()
    if cg:
        return cg
    pn = (os.environ.get("PROJECT_NAME") or "").strip()
    if not pn:
        return None
    try:
        sys.path.insert(0, os.getcwd())
        from vco_lib.project_naming import canonical_class_prefix

        return canonical_class_prefix(pn)
    except Exception:
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", pn)
        if sanitized and sanitized[0].isalpha() and not sanitized[0].isupper():
            sanitized = sanitized[0].upper() + sanitized[1:]
        return sanitized or None


def _connect():
    """Connect to Weaviate the SAME way the analyzer + 5_to_6 do (custom
    host/port). Reads ``WEAVIATE_URL`` (default localhost:8081) + ``GRPC_PORT``
    (default 50052). Raises on failure (→ non-zero exit → runner defers/retries).
    """
    import weaviate  # local import: only needed when the edge actually runs

    url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
    m = re.match(r"^(https?)://([^:/]+)(?::(\d+))?", url)
    scheme, host, port = ("http", "localhost", 8081)
    if m:
        scheme = m.group(1)
        host = m.group(2)
        port = int(m.group(3)) if m.group(3) else (443 if scheme == "https" else 8081)
    grpc_port = int(os.environ.get("GRPC_PORT") or os.environ.get("WEAVIATE_GRPC_PORT") or 50052)
    return weaviate.connect_to_custom(
        http_host=host,
        http_port=port,
        http_secure=(scheme == "https"),
        grpc_host=host,
        grpc_port=grpc_port,
        grpc_secure=(scheme == "https"),
    )


def _purge_transient_rows(coll, path_prop: str) -> "tuple[int, int]":
    """Delete every row in ``coll`` whose ``path_prop`` VALUE literally contains
    ``.claude/state/`` — matched as an EXACT PYTHON SUBSTRING on the value read
    back, NEVER via a Weaviate ``Like`` (see ``_TRANSIENT_MARKER`` for the
    tokenization safety rationale).

    Reads ``path_prop`` for every object, tests the raw string in Python, and
    ``delete_by_id``s ONLY the confirmed-garbage UUIDs. No re-embed, no
    re-summarize, no ``replace()`` — survivors are never touched.

    Returns ``(deleted, failures)``. Per-row deletes are best-effort (a single
    failure logs + continues so a transient error can't wedge the edge), but the
    failure COUNT is propagated so ``main`` can exit non-zero on a half-applied
    edge → the runner does NOT advance the recorded version and retries next
    update (the leftover rows are the pre-fix status quo, never a regression).
    """
    # Defense-in-depth: a class missing this property (e.g. a schema variant, or
    # CodeAPI/CodeInteraction if a caller ever mis-maps them) would 500 the
    # iterator. Confirm the prop exists first; skip cleanly if not (nothing to
    # purge there — those classes are not file-anchored on this property).
    try:
        cfg = coll.config.get()
        if path_prop not in {p.name for p in cfg.properties}:
            return 0, 0
    except Exception:  # noqa: BLE001 — config probe is best-effort; fall through
        pass

    to_delete = []
    for obj in coll.iterator(return_properties=[path_prop]):
        val = (obj.properties or {}).get(path_prop) or ""
        # EXACT substring on the raw value — NOT a tokenized Like. A real
        # `.claude/hooks/…` / `.claude/scripts/…` source path does not contain
        # `.claude/state/` and is therefore preserved.
        if _TRANSIENT_MARKER in val:
            to_delete.append(obj.uuid)

    deleted = 0
    failures = 0
    for uid in to_delete:
        try:
            coll.data.delete_by_id(uuid=str(uid))
            deleted += 1
        except Exception as exc:  # noqa: BLE001 — never wedge on one row
            failures += 1
            print(
                f"6_to_7: delete_by_id failed for {uid} in "
                f"{getattr(coll, 'name', '?')}: {exc}",
                file=sys.stderr,
            )
    return deleted, failures


def main() -> int:
    prefix = _resolve_codegraph_prefix()
    if not prefix:
        print("6_to_7: no CODE_GRAPH_PROJECT / PROJECT_NAME in env; nothing to purge")
        return 0

    try:
        client = _connect()
    except Exception as exc:  # Weaviate unreachable → defer + retry next update.
        print(f"6_to_7: cannot connect to Weaviate ({exc}); deferring", file=sys.stderr)
        return 1

    total = 0
    total_failures = 0
    try:
        for base, path_prop in _CLASS_PATH_PROP.items():
            class_name = f"{prefix}_{base}"
            try:
                if not client.collections.exists(class_name):
                    continue
                coll = client.collections.get(class_name)
                n, fails = _purge_transient_rows(coll, path_prop)
                total += n
                total_failures += fails
                if n:
                    print(f"6_to_7: purged {n} .claude/state rows from {class_name}")
            except Exception as exc:
                # A genuine enumerate/connection error on a class is a real
                # failure — the runner must NOT advance the version on a half-
                # applied edge (leftover rows are the pre-fix status quo; retry
                # next update).
                print(
                    f"6_to_7: purge failed on {class_name}: {exc}",
                    file=sys.stderr,
                )
                return 1
        if total_failures:
            # Some confirmed-garbage rows could not be deleted → do NOT advance
            # the version; next update re-runs and retries the leftovers.
            print(
                f"6_to_7: {total_failures} delete(s) FAILED "
                f"({total} removed) — deferring version advance",
                file=sys.stderr,
            )
            return 1
        print(f"6_to_7: transient-scratch purge complete ({total} rows removed)")
    finally:
        try:
            client.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
