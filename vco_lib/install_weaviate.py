# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Weaviate KG-row hygiene for install.py (vco_lib.install_weaviate — v0.2.73).

IN-1 (v0.2.73) extraction: the install-time Knowledge-Graph row-hygiene
slice — content-hash diffing (the "pay once, never again" incremental-embed
gate) and orphan-row pruning (delete Weaviate rows whose ``file_path`` no
longer exists on disk). This is the cohesive "did the KG drift from disk?"
family that ran inside ``_seed_weaviate``'s full-sync branch.

The functions here take their install.py couplings as EXPLICIT parameters
rather than reaching into install.py's module state:

* ``project_root`` — the resolved project/install root (install.py's
  ``PROJECT_ROOT``). Passed in so the monkeypatch contract that the test
  suite relies on (patching ``install.PROJECT_ROOT``) keeps flowing through
  the thin install.py wrappers.
* ``log_event`` — optional ``(step, phase, detail, *, data=None)`` logger
  (install.py passes its ``_log_install_event``; other callers pass None).
  Same shape as ``vco_lib.project_init.rebuild_collections``'s ``log_event``.
* ``is_orchestrator_root_install`` — callable returning whether this is an
  orchestrator self-install (drives the prune dry-run default).

install.py keeps thin same-signature wrappers over these so its existing
call sites — and the ``install.<name>`` accessors the test suite uses —
resolve unchanged. This module does NOT import install.py: the dependency
edge is one-directional (install.py → vco_lib.install_weaviate), which
avoids the import cycle a back-import would create (install.py runs
top-level configuration code at import time).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional


def _batch_query_weaviate_content_hashes(
    collection_name: str,
    weaviate_url: str,
    *,
    log_event: Optional[Callable] = None,
) -> "dict[str, str]":
    """Thin wrapper around ``vco_lib.kg_sync.batch_query_content_hashes``.

    v0.2.46 KG-AUTO-HEAL-E: this function used to host the full V46-A
    safety-triad implementation (no Like-% / limit:10000 / errors-before-
    data / saturation warning). It was extracted into
    ``vco_lib/kg_sync.py`` so the v0.2.46 KG-rebind re-sync path can
    share the exact same hardened code path (single source of truth +
    one regression-guard target). See plan §9.6 + the V46 audit at
    ``.claude/context/audits/v0.2.46-compat-V46-2026-06-04.md`` (top-of-
    report "CRITICAL: WATCH OUT FOR" item #1).

    This wrapper preserves the legacy ``(collection_name, weaviate_url)``
    positional signature so install.py's existing call sites keep working
    without modification. It also keeps the existing ``log_event``
    observability channel — the new helper's ``on_warn`` callback is mapped
    to install-time deferral log entries.

    Original docstring preserved for historical reference:

      Returns a dict mapping ``file_path`` → ``content_hash`` for every
      object in the collection that has both properties populated.
      Objects without a content_hash (e.g. created before v0.2.17) return
      an empty string for that file_path — the diff logic treats this
      as "always stale" for that file, which triggers a single-file
      re-sync (correct: we want to fill in the missing hash).

      v0.2.42 CI-10: "pay once, never again" — this function is the key
      enabler. Once hashes are stored in Weaviate (after the first sync
      that sets content_hash), subsequent ``--update`` runs only embed
      changed files. Nodes created before v0.2.17 (no content_hash
      property) will be re-synced once (to populate content_hash), then
      skipped forever.

      v0.2.46 V46-A (now in ``vco_lib.kg_sync``): dropped the broken
      ``where: Like "%"`` filter, bumped ``limit`` 1000 → 10000, and
      inspects ``body["errors"]`` BEFORE consuming ``data``. See
      ``knowledge/concepts/silent-zero-fallback-antipattern.md``
      instance #3.
    """
    from vco_lib.kg_sync import batch_query_content_hashes

    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is not None:
            try:
                log_event(step, phase, detail, data=data)
            except TypeError:
                # Older log_event signatures may not accept `data` kwarg.
                log_event(step, phase, detail)

    def _on_warn(channel: str, payload: "dict") -> None:
        # Map the helper's structured warn channels back into the
        # legacy CI-10 install-event log so existing dashboards /
        # `UPDATE_DEFERRED.md` parsing keeps working.
        if channel == "graphql_errors":
            errs = payload.get("errors", [])
            first = errs[0] if errs else "unknown"
            _log(
                "7c/10", "warn",
                f"CI-10: GraphQL errors for {collection_name!r}: {first[:200]}",
                data={"collection": collection_name, "errors": errs},
            )
        elif channel == "saturation":
            _log(
                "7c/10", "warn",
                f"CI-10: hit Weaviate QUERY_MAXIMUM_RESULTS cap (10000) for "
                f"{collection_name!r}; some rows may be missing — consider "
                f"cursor pagination",
                data={"collection": collection_name, "rows": payload.get("rows", 0)},
            )
        elif channel == "transport_failure":
            errs = payload.get("errors", [])
            first = errs[0] if errs else "unknown"
            _log(
                "7c/10", "warn",
                f"CI-10: batch hash query failed for {collection_name!r}: {first[:200]}",
                data={"collection": collection_name, "error": first[:200]},
            )

    return batch_query_content_hashes(
        weaviate_url=weaviate_url,
        collection_name=collection_name,
        on_warn=_on_warn,
    )


def _compute_on_disk_content_hashes(knowledge_root: Path) -> "dict[str, str]":
    """Compute _content_signature_excluding_updated for every .md in knowledge/.

    Returns a dict mapping relative_file_path (str, relative to the project
    root) → content_signature. The relative path matches the file_path stored
    in Weaviate by sync_knowledge_graph.py (which uses
    `file_path.relative_to(PROJECT_ROOT)` or the absolute path, depending on
    KG_BASE_DIR — but content_hash comparison is by file content, so we can
    detect drift purely by hash comparison regardless of the stored path format
    as long as we match the key consistently).
    """
    import hashlib
    import re as _re

    def _sig(text: str) -> str:
        """Mirror of sync_knowledge_graph.py::_content_signature_excluding_updated."""
        if not text.strip().startswith("---"):
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
        parts = text.split("---", 2)
        if len(parts) < 3:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
        fm_no_updated = _re.sub(r"^updated:.*$\n?", "", parts[1], flags=_re.MULTILINE)
        return hashlib.sha256((fm_no_updated + parts[2]).encode("utf-8")).hexdigest()

    result: dict[str, str] = {}
    if not knowledge_root.exists():
        return result
    for md_file in knowledge_root.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            result[str(md_file)] = _sig(content)
        except OSError:
            # Unreadable file — include with empty hash so the diff logic
            # treats it as stale (forces a re-sync attempt).
            result[str(md_file)] = ""
    return result


def _safe_inside_project(candidate: "Path", project_root: Path) -> bool:
    """v0.2.44 V44-H: return True iff candidate resolves to a path inside
    project_root. Defends against stored file_path values containing '..'
    that would otherwise escape the project tree (e.g. "../../../etc/passwd"
    would resolve to an existing file outside project_root and erroneously
    keep an orphan KG row alive forever).

    Resolve both sides so symlinked install dirs compare consistently. Soft
    fail (return False) on any OS / value error — a failed resolution should
    behave like "outside the project" so the prune is permitted to delete
    the stale row.
    """
    try:
        resolved = candidate.resolve()
        project_resolved = project_root.resolve()
        return resolved.is_relative_to(project_resolved)
    except (OSError, ValueError):
        return False


def _path_resolves_on_disk(file_path_str: str, project_root: Path) -> bool:
    """v0.2.44 V44-A: try multiple normalization strategies before declaring orphan.

    Strategies (any True → file exists, do NOT prune):
      1. Direct relative-to-project_root (must resolve INSIDE project_root)
      2. Resolve project_root first (handles symlinked install dirs;
         must resolve INSIDE project_root)
      3. Absolute path directly (intentionally bypasses project_root —
         bundled-template absolute paths are legitimately outside)
      4. Strip .claude/worktrees/agent-XXX/ prefix (must resolve INSIDE
         project_root)

    v0.2.44 V44-H: strategies 1, 2, 4 now require the resolved path to
    lie inside project_root. Previously, a stored value like
    "../../../etc/passwd" would resolve to an existing file outside the
    project tree and keep a corrupted KG row alive forever (closes Adv-1
    P1-1 + P1-2).
    """
    # 1. Direct relative-to-project_root (canonical)
    try:
        candidate = project_root / file_path_str
        if _safe_inside_project(candidate, project_root) and candidate.exists():
            return True
    except (OSError, ValueError):
        # NUL bytes / invalid chars in stored file_path would crash Path.exists()
        pass
    # 2. Resolve project_root first (handles symlinked install dirs)
    try:
        candidate = project_root.resolve() / file_path_str
        if _safe_inside_project(candidate, project_root) and candidate.exists():
            return True
    except (OSError, ValueError):
        pass
    # 3. Absolute path directly — intentionally bypasses project_root
    # semantics. Bundled-template absolute paths (e.g. paths written by
    # an absolute-mode sync_knowledge_graph.py run) are legitimately
    # outside project_root and must continue to count as on-disk.
    try:
        candidate = Path(file_path_str)
        if candidate.is_absolute() and candidate.exists():
            return True
    except (OSError, ValueError):
        pass
    # 4. Strip worktree prefix (.claude/worktrees/agent-XXX/foo.md → foo.md)
    if ".claude/worktrees/agent-" in file_path_str:
        import re
        m = re.search(r"\.claude/worktrees/agent-[^/]+/(.*)$", file_path_str)
        if m:
            candidate = project_root / m.group(1)
            if _safe_inside_project(candidate, project_root) and candidate.exists():
                return True
    return False


def _prune_stale_kg_rows(
    collection_name: str,
    weaviate_url: str,
    *,
    dry_run: bool | None = None,
    project_root: Path,
    log_event: Optional[Callable] = None,
    is_orchestrator_root_install: Optional[Callable[[], bool]] = None,
) -> None:
    """V0243-6: delete Weaviate objects whose ``file_path`` has no matching
    on-disk Markdown file in ``knowledge/**/*.md``.

    Called from the full-sync branch of ``_seed_weaviate`` after a successful
    ``sync_knowledge_graph.py --all``.  The sync upserts every on-disk file
    but never deletes rows for files that were removed from disk — this step
    closes that gap.

    Args:
        collection_name: The Weaviate collection to prune (``KG_COLLECTION``).
        weaviate_url: Base URL of the Weaviate instance.
        dry_run: When True, print the stale count but do NOT delete.
                 When False, batch-delete all stale rows via the Weaviate
                 ``/v1/batch/objects`` endpoint.
                 When None (default): False for orchestrator-root installs
                 (``is_orchestrator_root_install()``), True otherwise.
        project_root: Resolved project/install root (install.py's PROJECT_ROOT).
        log_event: Optional install-time logger, same shape as install.py's
                 ``_log_install_event``.
        is_orchestrator_root_install: Callable returning whether this is an
                 orchestrator self-install; drives the ``dry_run=None`` default.

    Soft-fail throughout: any error is logged but never raises into the caller.
    """
    def _log(step: str, phase: str, detail: str = "", *, data=None) -> None:
        if log_event is not None:
            try:
                log_event(step, phase, detail, data=data)
            except TypeError:
                log_event(step, phase, detail)

    if dry_run is None:
        dry_run = not (
            is_orchestrator_root_install() if is_orchestrator_root_install else False
        )

    # v0.2.44 V44-A: existence is checked via _path_resolves_on_disk per row
    # below (multi-strategy: relative, resolved, absolute, worktree-stripped).
    # The earlier all_on_disk set-build is no longer needed.

    # Fetch all stored (uuid, file_path) pairs from Weaviate.
    stored: list[tuple[str, str]] = []  # (uuid, file_path)
    try:
        base = (weaviate_url or "http://localhost:8081").rstrip("/")
        # v0.2.46 V46-A: dropped the broken `where: Like "%"` filter (same
        # bug as CI-10 in _batch_query_weaviate_content_hashes — Weaviate's
        # BM25 tokenizer rejects `%` as "only stopwords provided" and the
        # null response was silently coalesced to []). With the filter
        # dropped, the secondary `{ Get { ... } }` brace-balance issue that
        # Investigator 1 reproduced live ("Expected Name, found EOF") also
        # disappears because the string is now syntactically simpler.
        # Bumped limit 2000 → 10000 (Weaviate's QUERY_MAXIMUM_RESULTS
        # default); saturation warning below signals if we approach the cap.
        gql_query = (
            f"{{ Get {{ {collection_name}(limit: 10000) "
            f"{{ _additional {{ id }} file_path }} }} }}"
        )
        import json as _json
        import urllib.request as _ur
        data = _json.dumps({"query": gql_query}).encode()
        req = _ur.Request(
            f"{base}/v1/graphql",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=15) as resp:  # noqa: S310
            body = _json.loads(resp.read())

        # v0.2.46 V46-A: inspect errors array BEFORE consuming data. See
        # knowledge/concepts/mcp-loud-fail-error-pattern.md § GraphQL
        # errors[] array. Non-empty errors → WARN + early return (prune
        # check is best-effort; no destructive action is taken without
        # an authoritative stored-rows list).
        if body.get("errors"):
            first_err = (body["errors"][0] or {}).get("message", "unknown")
            _log(
                "7c/10", "warn",
                f"V0243-6: GraphQL errors fetching prune candidates from "
                f"{collection_name!r}: {first_err[:200]}",
                data={
                    "collection": collection_name,
                    "errors": [
                        (e or {}).get("message", "")[:200]
                        for e in body["errors"][:3]
                    ],
                },
            )
            return

        objects = (
            body.get("data", {})
            .get("Get", {})
            .get(collection_name, [])
        ) or []

        # v0.2.46 V46-A: saturation warning — same caveat as CI-10. If we
        # hit the cap, the prune set is INCOMPLETE; aborting is safer than
        # deleting based on a truncated view (we might mark live rows as
        # stale because they fell past the limit).
        if len(objects) >= 10000:
            _log(
                "7c/10", "warn",
                f"V0243-6: hit Weaviate QUERY_MAXIMUM_RESULTS cap (10000) "
                f"for {collection_name!r}; aborting prune to avoid "
                f"false-positives — consider cursor pagination",
                data={"collection": collection_name, "rows": len(objects)},
            )
            return

        for obj in objects:
            uid = (obj.get("_additional") or {}).get("id") or ""
            fp = (obj.get("file_path") or "").strip()
            if uid and fp:
                stored.append((uid, fp))
    except Exception as exc:
        _log(
            "7c/10", "warn",
            f"V0243-6: could not fetch stored file_paths for prune check: {exc}",
            data={"collection": collection_name, "error": str(exc)[:200]},
        )
        return

    stale_uuids: list[str] = []
    stale_paths: list[str] = []
    for uid, fp in stored:
        if not _path_resolves_on_disk(fp, project_root):
            stale_uuids.append(uid)
            stale_paths.append(fp)
            print(f"    → pruned orphan row file_path={fp!r}")

    if not stale_uuids:
        _log(
            "7c/10", "ok",
            f"V0243-6: prune check: no stale rows in {collection_name!r} "
            f"({len(stored)} rows, all match on-disk)",
            data={"collection": collection_name, "stored": len(stored), "stale": 0},
        )
        return

    print(
        f"  V0243-6: {len(stale_uuids)} stale KG row(s) found "
        f"(file deleted from disk) in {collection_name!r}"
    )
    _log(
        "7c/10", "info",
        f"V0243-6: prune: {len(stale_uuids)} stale row(s) in {collection_name!r}",
        data={"collection": collection_name, "stale": len(stale_uuids),
              "dry_run": dry_run},
    )

    if dry_run:
        print(f"  (dry-run: {len(stale_uuids)} row(s) would be deleted)")
        return

    # Batch-delete via Weaviate v1 batch/objects endpoint.
    # v0.2.46 V46-A-followup: SECOND v0.2.43 bug caught by V46-B's live
    # integration test — `valueText: <list>` returns HTTP 400 ("cannot
    # unmarshal array into Go struct field of type string"). The
    # correct field for `ContainsAny` with a list of UUIDs is
    # `valueTextArray` (plural). Reproduced live 2026-06-03; the buggy
    # form has been on disk since V0243-6 shipped in v0.2.43 (which is
    # ALSO why the prune logic appeared to "no-op silently" — same
    # silent-zero-fallback antipattern as the diff-gate fetch).
    try:
        base = (weaviate_url or "http://localhost:8081").rstrip("/")
        import json as _json
        import urllib.request as _ur
        delete_body = _json.dumps({
            "match": {
                "class": collection_name,
                "where": {
                    "path": ["id"],
                    "operator": "ContainsAny",
                    "valueTextArray": stale_uuids,
                },
            },
            "output": "minimal",
            "dryRun": False,
        }).encode()
        req = _ur.Request(
            f"{base}/v1/batch/objects",
            data=delete_body,
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        with _ur.urlopen(req, timeout=30) as resp:  # noqa: S310
            result_body = _json.loads(resp.read())
        deleted_count = (result_body.get("results") or {}).get("successful", 0)
        print(f"  V0243-6: pruned {deleted_count} stale row(s)")
        _log(
            "7c/10", "ok",
            f"V0243-6: pruned {deleted_count} stale row(s) from {collection_name!r}",
            data={"collection": collection_name, "deleted": deleted_count},
        )
    except Exception as exc:
        _log(
            "7c/10", "warn",
            f"V0243-6: batch delete failed for {collection_name!r}: {exc}",
            data={"collection": collection_name, "error": str(exc)[:200]},
        )
