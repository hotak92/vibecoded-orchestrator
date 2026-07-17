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


# ─── v0.2.84 (D5/P3): honest orphan-collection reference check ────────────────
#
# The keys whose values may point at a Development collection on any env
# surface. DEVELOPMENT_COLLECTION is the direct pointer; KG_COLLECTION is the
# sibling from which the paired Development name is derived by suffix-swap
# (``<X>_KnowledgeGraph`` → ``<X>_Development``), so a KG surface that names the
# candidate's own KG prefix is ALSO a reference (P2's repoint will converge the
# dev pointer to match it).
_DEV_REFERENCE_KEYS = ("DEVELOPMENT_COLLECTION", "KG_COLLECTION")


def _dev_from_kg_name(kg_name: str) -> Optional[str]:
    """Suffix-swap ``<X>_KnowledgeGraph`` → ``<X>_Development`` (the one dev
    derivation rule; returns ``None`` for a non-``_KnowledgeGraph`` name)."""
    suffix = "_KnowledgeGraph"
    if kg_name.endswith(suffix):
        return kg_name[: -len(suffix)] + "_Development"
    return None


def _managed_env_value(text: str, key: str) -> Optional[str]:
    """Extract ``export KEY="value"`` (or ``KEY=value``) from the ``.claude/env``
    managed block in ``text``; ``None`` when absent.

    CRLF-safe (A3/A4): splits on universal newlines and ``.strip()``s each line
    so a Windows ``\\r\\n``-terminated managed block parses identically to a
    ``\\n`` one. Scoped to the managed region only (between the shared
    BEGIN/END sentinels) so a user's own out-of-block export can never be read
    as a VCO-managed value. Strips one matching pair of single/double quotes;
    NO variable expansion. First match wins.

    v0.2.84 NOTE: dedup candidate with ``vco_lib.project_init``'s managed-block
    scan (:8615 ``_has_user_secret_shaped_line``) and
    ``vco_lib.agent_secrets._parse_dotenv_value`` — the coordinator reconciles
    these onto ONE shared home at merge/fix-pass time. Kept local here for the
    ownership walls this cycle (project_init.py is WP-4's, config_projection.py
    is WP-2's).
    """
    # Import the managed-block sentinels from the canonical home (read-only —
    # no ownership conflict; the exact idiom project_init's scan uses).
    try:
        from vco_lib.config_projection import (
            CLAUDE_ENV_MANAGED_BEGIN,
            CLAUDE_ENV_MANAGED_END,
        )
    except Exception:  # noqa: BLE001 — partial install: parse the whole text
        CLAUDE_ENV_MANAGED_BEGIN = None
        CLAUDE_ENV_MANAGED_END = None

    block = text
    if CLAUDE_ENV_MANAGED_BEGIN and CLAUDE_ENV_MANAGED_END:
        begin = text.find(CLAUDE_ENV_MANAGED_BEGIN)
        end = text.find(CLAUDE_ENV_MANAGED_END)
        if begin == -1 or end == -1 or end < begin:
            return None  # no managed block → nothing VCO-managed to read
        block = text[begin:end]

    for line in block.splitlines():  # universal-newline split → CRLF-safe
        s = line.strip()  # trailing \r stripped here too
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        return v
    return None


def _settings_json_env_value(text: str, key: str) -> Optional[str]:
    """Read ``env[key]`` from a ``.claude/settings.json`` document; ``None`` on
    absence or any parse error (soft-fail)."""
    import json as _json

    try:
        data = _json.loads(text)
    except Exception:  # noqa: BLE001 — malformed settings.json → no reference
        return None
    if not isinstance(data, dict):
        return None
    env_block = data.get("env")
    if not isinstance(env_block, dict):
        return None
    value = env_block.get(key)
    return value if isinstance(value, str) and value else None


def dev_collection_is_referenced(
    candidate: str, folder: Path,
) -> "tuple[bool, str]":
    """v0.2.84 (D5/P3): is the Development collection ``candidate`` referenced
    by ANY live configuration surface? Returns ``(referenced, surface)`` where
    ``surface`` names WHERE the reference was found (empty when not referenced).

    Consulted surfaces (first hit wins — the honest orphan detector must not
    claim "no callers" while a surface literally names the collection):

      (a) the root's ``<folder>/.claude/settings.json`` ``env`` block —
          DEVELOPMENT_COLLECTION==candidate, OR KG_COLLECTION whose suffix-swap
          sibling ==candidate (P2 will repoint the dev pointer to match).
      (b) the root's ``<folder>/.claude/env`` managed block — same two keys,
          CRLF-safe managed-block parse.
      (c) the process env (``DEVELOPMENT_COLLECTION``/``KG_COLLECTION``) — the
          existing pre-v0.2.84 check, kept and generalized to the sibling rule.
      (d) launcher.db resolution when reachable — the orchestrator-root
          project's DB-resolved DEVELOPMENT_COLLECTION (via the read-only
          config_projection projection, the SAME rule the hub serves). This
          catches the case where the on-disk env hasn't been repointed yet but
          the binding already names the paired dev collection.

    Soft-fail throughout: an unreadable file / unreachable DB / import error on
    ONE surface never raises — it simply contributes no reference (the next
    surface is still consulted). A ``candidate`` that is empty/whitespace is
    treated as un-referenced (nothing to match).
    """
    candidate = (candidate or "").strip()
    if not candidate:
        return (False, "")
    folder = Path(folder)

    def _matches(dev_value: Optional[str], kg_value: Optional[str]) -> bool:
        if dev_value and dev_value.strip() == candidate:
            return True
        if kg_value and _dev_from_kg_name(kg_value.strip()) == candidate:
            return True
        return False

    # (a) settings.json env
    settings_path = folder / ".claude" / "settings.json"
    if settings_path.is_file():
        try:
            text = settings_path.read_text(encoding="utf-8")
            if _matches(
                _settings_json_env_value(text, "DEVELOPMENT_COLLECTION"),
                _settings_json_env_value(text, "KG_COLLECTION"),
            ):
                return (True, ".claude/settings.json::env")
        except Exception:  # noqa: BLE001 — soft-fail this surface only
            pass

    # (b) .claude/env managed block
    env_path = folder / ".claude" / "env"
    if env_path.is_file():
        try:
            text = env_path.read_text(encoding="utf-8")
            if _matches(
                _managed_env_value(text, "DEVELOPMENT_COLLECTION"),
                _managed_env_value(text, "KG_COLLECTION"),
            ):
                return (True, ".claude/env managed block")
        except Exception:  # noqa: BLE001 — soft-fail this surface only
            pass

    # (c) process env (the existing pre-v0.2.84 check, generalized to sibling).
    import os as _os

    if _matches(
        _os.environ.get("DEVELOPMENT_COLLECTION"),
        _os.environ.get("KG_COLLECTION"),
    ):
        return (True, "process env")

    # (d) launcher.db resolution (read-only) when reachable. Resolve the
    # orchestrator-root project's DB-projected DEVELOPMENT_COLLECTION/KG the
    # hub would serve and compare. Best-effort: any import/DB error → skip.
    try:
        from vco_lib import config_projection as _cp

        db_path = _cp._resolve_launcher_db_path()
        if db_path and Path(db_path).is_file():
            # Find the project whose folder is this orchestrator root.
            # ``list_registered_projects`` yields ``{id, name, slug,
            # folder_path, folder}`` dicts.
            for proj in _cp.list_registered_projects(db_path=db_path):
                if not isinstance(proj, dict):
                    continue
                pfolder = proj.get("folder_path") or proj.get("folder")
                pid = proj.get("id")
                if not pfolder or not pid:
                    continue
                try:
                    same = Path(pfolder).resolve() == folder.resolve()
                except Exception:  # noqa: BLE001
                    same = str(pfolder) == str(folder)
                if not same:
                    continue
                bundle = _cp.project_env_from_db(pid, db_path=db_path)
                # ProjectEnvBundle is a TypedDict with the flat map under
                # ``canonical_env`` (never an ``.env`` attribute).
                env_map = None
                if isinstance(bundle, dict):
                    env_map = bundle.get("canonical_env")
                if isinstance(env_map, dict) and _matches(
                    env_map.get("DEVELOPMENT_COLLECTION"),
                    env_map.get("KG_COLLECTION"),
                ):
                    return (True, "launcher.db resolution")
                break  # matched the root project row; no need to scan further
    except Exception:  # noqa: BLE001 — DB unreachable / partial → skip surface
        pass

    return (False, "")


def paired_dev_sibling(candidate: str, folder: Path) -> Optional[str]:
    """Resolve the CONFIGURED Development collection name (the sibling the docs
    actually live in) for enriching the orphan deferral, or ``None`` when it is
    the same as ``candidate`` / cannot be resolved.

    Reads the same surfaces as :func:`dev_collection_is_referenced` in priority
    order (settings.json env → .claude/env → process env → launcher.db),
    preferring an explicit DEVELOPMENT_COLLECTION and falling back to the
    KG-derived sibling. Returns the first configured dev name that DIFFERS from
    ``candidate`` (the orphan) — that is the data-holding target worth naming.
    Soft-fails to ``None``.
    """
    candidate = (candidate or "").strip()
    folder = Path(folder)

    def _pick(dev_value: Optional[str], kg_value: Optional[str]) -> Optional[str]:
        if dev_value and dev_value.strip() and dev_value.strip() != candidate:
            return dev_value.strip()
        if kg_value:
            derived = _dev_from_kg_name(kg_value.strip())
            if derived and derived != candidate:
                return derived
        return None

    # (a) settings.json env
    settings_path = folder / ".claude" / "settings.json"
    if settings_path.is_file():
        try:
            text = settings_path.read_text(encoding="utf-8")
            got = _pick(
                _settings_json_env_value(text, "DEVELOPMENT_COLLECTION"),
                _settings_json_env_value(text, "KG_COLLECTION"),
            )
            if got:
                return got
        except Exception:  # noqa: BLE001
            pass

    # (b) .claude/env managed block
    env_path = folder / ".claude" / "env"
    if env_path.is_file():
        try:
            text = env_path.read_text(encoding="utf-8")
            got = _pick(
                _managed_env_value(text, "DEVELOPMENT_COLLECTION"),
                _managed_env_value(text, "KG_COLLECTION"),
            )
            if got:
                return got
        except Exception:  # noqa: BLE001
            pass

    # (c) process env
    import os as _os

    got = _pick(
        _os.environ.get("DEVELOPMENT_COLLECTION"),
        _os.environ.get("KG_COLLECTION"),
    )
    if got:
        return got

    # (d) launcher.db resolution
    try:
        from vco_lib import config_projection as _cp

        db_path = _cp._resolve_launcher_db_path()
        if db_path and Path(db_path).is_file():
            for proj in _cp.list_registered_projects(db_path=db_path):
                if not isinstance(proj, dict):
                    continue
                pfolder = proj.get("folder_path") or proj.get("folder")
                pid = proj.get("id")
                if not pfolder or not pid:
                    continue
                try:
                    same = Path(pfolder).resolve() == folder.resolve()
                except Exception:  # noqa: BLE001
                    same = str(pfolder) == str(folder)
                if not same:
                    continue
                bundle = _cp.project_env_from_db(pid, db_path=db_path)
                env_map = bundle.get("canonical_env") if isinstance(bundle, dict) else None
                if isinstance(env_map, dict):
                    got = _pick(
                        env_map.get("DEVELOPMENT_COLLECTION"),
                        env_map.get("KG_COLLECTION"),
                    )
                    if got:
                        return got
                break
    except Exception:  # noqa: BLE001
        pass

    return None


def build_orphan_dev_deferral(
    candidate: str,
    folder: Path,
    weaviate_url: str,
    class_map: dict,
    count_fn: Callable[[str, str], "Optional[int]"],
    *,
    log_event: Optional[Callable] = None,
):
    """v0.2.84 (D5/P3): decide whether the orphan-Development-collection
    deferral should be emitted, and build it. Returns a ``DeferralEntry`` when
    ``candidate`` is present in ``class_map``, UNreferenced by any live config
    surface, and holds 0 rows; ``None`` otherwise (present-but-referenced,
    non-empty, absent, or undeterminable row count).

    This is the whole (a) branch of install.py's
    ``_emit_orchestrator_root_schema_deferrals`` (the install.py ratchet keeps
    real logic in vco_lib). ``count_fn(weaviate_url, class_name) -> Optional[int]``
    is install.py's ``_count_weaviate_class_objects`` (``None`` = unknown, never
    treated as 0). ``log_event`` is the optional install-time logger.

    Honest states:
      * present + referenced ⇒ log "referenced by <surface> — not an orphan"
        and return None (P2's repoint will converge the pointer).
      * present + unreferenced + 0 rows ⇒ build + return the entry, enriched
        with the binding-paired sibling collection when it exists and holds
        rows (the real target the docs live in).
      * present + unreferenced + non-zero/unknown rows ⇒ return None (not empty
        / can't confirm empty — never a destructive drop we can't justify).

    Conservative default: an unexpected error in the reference check is treated
    as REFERENCED (returns None) so a destructive-drop deferral is never emitted
    on a check we could not positively complete.
    """
    def _log(step: str, phase: str, detail: str = "") -> None:
        if log_event is not None:
            try:
                log_event(step, phase, detail)
            except TypeError:  # logger with a data= kwarg
                log_event(step, phase, detail, data=None)

    candidate = (candidate or "").strip()
    if not candidate or candidate not in class_map:
        return None

    try:
        referenced, ref_surface = dev_collection_is_referenced(candidate, folder)
    except Exception as exc:  # noqa: BLE001 — reference check must never wedge
        referenced, ref_surface = (True, f"check-error ({exc})")
    if referenced:
        _log(
            "7e/10", "info",
            f"V0243-13(a): {candidate!r} is referenced by {ref_surface} — "
            f"NOT an orphan (P2 repoint will converge it); no deferral emitted",
        )
        return None

    row_count = count_fn(weaviate_url, candidate)
    if row_count is None or row_count != 0:
        return None

    # Enrichment: name the binding-paired sibling when it exists and holds rows.
    sibling_note = ""
    try:
        sibling = paired_dev_sibling(candidate, folder)
    except Exception:  # noqa: BLE001
        sibling = None
    if sibling and sibling in class_map:
        sib_rows = count_fn(weaviate_url, sibling)
        if sib_rows is not None and sib_rows > 0:
            sibling_note = (
                f"  The binding-paired Development collection `{sibling}` is "
                f"the configured target and holds {sib_rows} row(s)."
            )

    from vco_lib.deferral_report import DeferralEntry

    _log(
        "7e/10", "info",
        f"V0243-13(a): orphan {candidate!r} deferral emitted (0 rows)",
    )
    return DeferralEntry(
        condition_id="orphan_orchestrator_development_collection",
        title=(
            f"Orphan Weaviate collection `{candidate}` has 0 rows and no callers"
        ),
        detected=(
            f"The Weaviate collection `{candidate}` exists at {weaviate_url} "
            f"with 0 stored objects.  It was created by older install.py "
            f"versions and is no longer populated (all docs/ sync now targets "
            f"per-project Development collections).  No live config surface "
            f"(settings.json env, .claude/env, process env, launcher.db) "
            f"references it.  Dropping it is safe.{sibling_note}"
        ),
        why_deferred=(
            "DROP is a destructive Weaviate operation even when the collection "
            "is empty — it cannot be undone without a full re-seed.  User "
            "consent required."
        ),
        command_to_apply=(
            f"# Delete the orphan collection via the Weaviate REST API:\n"
            f"curl -X DELETE {weaviate_url}/v1/schema/{candidate}\n"
            f"# Or open the Weaviate console at {weaviate_url} and delete the "
            f"class from the Schema tab."
        ),
        severity="info",
        kg_node_refs=[],
    )
