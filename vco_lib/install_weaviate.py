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

    Line-level parsing (CRLF-safe split, ``export`` prefix, quote strip, managed
    block scoping) is shared with ``vco_lib.agent_secrets._parse_dotenv_value``
    via ``vco_lib.envfile`` (v0.2.84 fix-pass — one concern, one home). Only the
    POLICY (key lookup, managed-block scope) stays here. (``project_init``'s
    secret-shape scan is a lookalike but a DIFFERENT parse contract — see the
    NOTE there — so it keeps its own regex and does not route through envfile.)
    """
    from vco_lib.envfile import env_value

    # Import the managed-block sentinels from the canonical home (read-only).
    # When they can't be resolved (partial install) fall back to parsing the
    # whole text — the historic behaviour.
    try:
        from vco_lib.config_projection import (
            CLAUDE_ENV_MANAGED_BEGIN,
            CLAUDE_ENV_MANAGED_END,
        )
    except Exception:  # noqa: BLE001 — partial install: parse the whole text
        CLAUDE_ENV_MANAGED_BEGIN = None
        CLAUDE_ENV_MANAGED_END = None

    return env_value(
        text, key,
        begin_marker=CLAUDE_ENV_MANAGED_BEGIN,
        end_marker=CLAUDE_ENV_MANAGED_END,
    )


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


# ---------------------------------------------------------------------------
# v0.2.89 FIX 1 — bounded Weaviate-readiness wait (gates the unbounded
# re-embed subprocess). Extracted here so install.py keeps a thin one-line
# wrapper (the install.py main()/total ratchet keeps real logic in vco_lib).
# ---------------------------------------------------------------------------


def wait_for_weaviate_ready(
    weaviate_url: str,
    deadline_seconds: float,
    *,
    print_fn: Optional[Callable[[str], None]] = None,
    progress_interval_s: float = 15.0,
    probe_timeout_s: float = 5.0,
    poll_interval_s: float = 2.0,
) -> bool:
    """Poll ``<weaviate_url>/v1/.well-known/ready`` with a BOUNDED deadline.

    v0.2.89 FIX 1. Returns True once the endpoint answers HTTP 200 within
    ``deadline_seconds``; returns False if the deadline elapses first. NEVER
    blocks indefinitely — that is the whole point (field report: a dead WSL2
    port-forward made Weaviate return HTTP 000, and the unbounded re-embed
    subprocess hung install.py forever on an unattended machine).

    Mirrors install.py's ``_wait_for_ollama`` bounded ``time.monotonic() +
    timeout`` pattern. Each probe carries its own short socket timeout so a
    hung port can't stall one iteration past the overall deadline.

    Pure except for the network probe + optional ``print_fn`` progress log;
    ``time``/``urllib`` are imported locally so non-Weaviate code paths don't
    pull them at module import.

    v0.2.89 review MINOR-6/7 hardening:
      * A ``ValueError`` from the probe (malformed URL — e.g. ``unknown url
        type``, invalid port) is DETERMINISTIC per URL, so re-probing until
        the deadline would burn the whole wait on a config error. Fail fast:
        return False immediately with a clear message.
      * ``http.client.HTTPException`` (e.g. ``BadStatusLine`` from a non-HTTP
        service squatting on the port) is transient-shaped — poll until the
        deadline instead of raising into the caller.
      * The probe response is closed explicitly on every path.
    """
    import http.client
    import time
    import urllib.error
    import urllib.request

    ready_url = f"{weaviate_url.rstrip('/')}/v1/.well-known/ready"
    deadline = time.monotonic() + deadline_seconds
    last_progress = time.monotonic()
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(ready_url, timeout=probe_timeout_s)
            try:
                status = getattr(resp, "status", None)
                if status == 200 or (status is None and resp.getcode() == 200):
                    return True
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001 — close must never wedge the wait
                    pass
        except ValueError as exc:
            # MINOR-6: malformed URL — deterministic, will never become ready.
            # Treat as unreachable IMMEDIATELY instead of burning the deadline.
            if print_fn is not None:
                try:
                    print_fn(
                        f"  ! Weaviate readiness probe aborted: malformed URL "
                        f"{ready_url!r} ({exc})"
                    )
                except Exception:  # noqa: BLE001 — logging must never wedge the wait
                    pass
            return False
        except urllib.error.HTTPError as exc:
            # Non-200 HTTP answer (4xx/5xx while Weaviate boots) — not ready
            # yet; close the error-response body and keep polling.
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            # Connection refused, DNS, socket timeout, HTTP 000 from a dead
            # port-forward, BadStatusLine from a non-HTTP service — all
            # "not ready yet"; keep polling until deadline.
            pass
        now = time.monotonic()
        if print_fn is not None and now - last_progress >= progress_interval_s:
            remaining = int(max(0, deadline - now))
            try:
                print_fn(
                    f"  ... waiting for Weaviate at {ready_url} "
                    f"(~{remaining}s left before soft-fail)"
                )
            except Exception:  # noqa: BLE001 — logging must never wedge the wait
                pass
            last_progress = now
        time.sleep(poll_interval_s)
    return False


# ---------------------------------------------------------------------------
# v0.2.89 FIX 2 — orphan legacy `.mcp.json` weaviate-kg block that shadows the
# migrated `.claude/settings.json`. Real logic lives here; install.py keeps a
# thin wrapper that supplies its logger.
# ---------------------------------------------------------------------------

# Env keys whose disagreement between `.mcp.json` and `.claude/settings.json`
# proves the `.mcp.json` weaviate-kg block is a stale pre-migration orphan.
MCP_JSON_STALE_ENV_KEYS = ("WEAVIATE_URL", "KG_COLLECTION", "SHARED_KG_COLLECTION")

# v0.2.89 review MAJOR-4.3: explicit user opt-out. When this env var is set to
# a truthy value ("1"/"true"/"yes"/"on"), the quarantine helper is a total
# no-op — for users who DELIBERATELY keep a divergent `.mcp.json` weaviate-kg
# env (e.g. pointing one project at a different Weaviate on purpose).
# Documented in docs/post-install/UPDATE-RECOVERY.md and in the deferral text.
MCP_JSON_QUARANTINE_SKIP_ENV = "VCO_SKIP_MCP_JSON_QUARANTINE"

# v0.2.89 review NIT-3: quarantine backups land under `.claude/context/`
# (keeps the project root clean of git-status noise). The restore-detection
# scan honours BOTH this location and legacy root-level `.mcp.json.bak-*`
# siblings written by earlier builds.
MCP_JSON_BACKUP_REL = Path(".claude") / "context"

# Loopback host aliases that are semantically the SAME endpoint for the URL
# comparison below. `[::1]` parses to hostname "::1" via urlsplit.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def weaviate_urls_equivalent(a: str, b: str) -> bool:
    """Semantic URL comparison for the `.mcp.json` staleness predicate.

    v0.2.89 review MAJOR-4.1: a raw string compare treated
    ``http://localhost:8081/`` vs ``http://localhost:8081``,
    ``http://127.0.0.1:8081`` vs ``http://localhost:8081``, and case
    differences as "demonstrable contradictions" — quarantining
    semantically-equal configs (and the 127.0.0.1 crowd is exactly the
    Windows demographic the quarantine targets).

    Normalization before comparing: scheme + host lowercased; loopback
    aliases (``localhost`` ≡ ``127.0.0.1`` ≡ ``[::1]``) collapse to one
    host; trailing ``/`` stripped from the path; port compared EXPLICITLY
    (an absent port resolves to the scheme default, 80/443). Query/fragment
    are ignored (never present on a Weaviate base URL; ignoring them is the
    false-negative-safe direction).

    False-NEGATIVES are the safe direction: when either side cannot be
    parsed (no host, invalid port, garbage), this returns True
    ("equivalent") so an unparseable URL can never count as the positive
    contradiction that triggers the quarantine.
    """
    from urllib.parse import urlsplit

    a = (a or "").strip()
    b = (b or "").strip()
    if a == b:
        return True

    def _norm(url: str) -> "tuple[str, str, int, str]":
        parts = urlsplit(url)
        scheme = (parts.scheme or "http").lower()
        host = (parts.hostname or "").lower()
        if not host:
            raise ValueError(f"no host in URL: {url!r}")
        if host in _LOOPBACK_HOSTS:
            host = "localhost"
        port = parts.port  # raises ValueError on an invalid port
        if port is None:
            port = 443 if scheme == "https" else 80
        return (scheme, host, port, parts.path.rstrip("/"))

    try:
        return _norm(a) == _norm(b)
    except (ValueError, AttributeError):
        # Unparseable on either side → cannot PROVE a contradiction →
        # equivalent (NOT stale). See docstring.
        return True


def resolve_settings_weaviate_env(project_root: Path) -> "Optional[dict]":
    """Return the migrated weaviate-relevant env from `.claude/settings.json`.

    VCO writes a TOP-LEVEL ``env`` block in ``.claude/settings.json`` (see
    install.py ``_build_vco_settings_defaults`` / ``_VCO_SETTINGS_MANAGED_KEYS``);
    the MCP subprocess inherits it. Returns a dict of the present keys in
    :data:`MCP_JSON_STALE_ENV_KEYS`, or ``None`` when the file is
    missing/unreadable / carries no comparable env — in which case the caller
    CANNOT prove staleness and must leave `.mcp.json` untouched.
    """
    import json

    settings_path = project_root / ".claude" / "settings.json"
    if not settings_path.is_file():
        return None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    env = data.get("env")
    if not isinstance(env, dict):
        return None
    out: dict = {}
    for key in MCP_JSON_STALE_ENV_KEYS:
        val = env.get(key)
        if isinstance(val, str):
            out[key] = val
    # Require at least one identity key to make a confident comparison. Without
    # a settings.json WEAVIATE_URL / KG_COLLECTION we cannot prove staleness.
    if "WEAVIATE_URL" not in out and "KG_COLLECTION" not in out:
        return None
    return out


def mcp_json_weaviate_env_is_stale(
    mcp_env: dict,
    settings_env: dict,
) -> "Optional[list]":
    """Return concrete reasons the `.mcp.json` weaviate-kg env is stale.

    Returns a non-empty list of reason strings when the ``.mcp.json``
    weaviate-kg env DEMONSTRABLY CONTRADICTS the migrated settings.json env
    (stale port, differing collection, or empty shared while settings has one).
    Returns ``None`` when there is NO positive contradiction (consistent →
    leave alone) — the conservative default.

    CONSERVATISM: only genuine contradictions count. A key ABSENT from
    ``.mcp.json`` is NOT a contradiction (weaviate-kg would inherit settings.json
    for it, EXCEPT shared — see below). A present-but-empty shared in `.mcp.json`
    counts as stale ONLY when settings.json has a non-empty shared (the exact
    Fabio case: shared-KG merge silently OFF).
    """
    if not isinstance(mcp_env, dict):
        return None
    reasons: list = []

    # WEAVIATE_URL: a present-and-differing value is the strongest signal
    # (Fabio: :8080 in .mcp.json vs :8081 in settings.json). v0.2.89 review
    # MAJOR-4.1: compared SEMANTICALLY via `weaviate_urls_equivalent` — a
    # trailing slash, a loopback alias (127.0.0.1 vs localhost), or a case
    # difference is NOT a contradiction; only a genuinely different endpoint
    # (host/port/path) counts.
    mcp_url = mcp_env.get("WEAVIATE_URL")
    set_url = settings_env.get("WEAVIATE_URL")
    if (
        isinstance(mcp_url, str)
        and mcp_url.strip()
        and isinstance(set_url, str)
        and set_url.strip()
        and not weaviate_urls_equivalent(mcp_url, set_url)
    ):
        reasons.append(
            f"WEAVIATE_URL {mcp_url.strip()!r} (.mcp.json) != "
            f"{set_url.strip()!r} (settings.json)"
        )

    # KG_COLLECTION: present-and-differing → stale routing.
    mcp_kg = mcp_env.get("KG_COLLECTION")
    set_kg = settings_env.get("KG_COLLECTION")
    if (
        isinstance(mcp_kg, str)
        and mcp_kg.strip()
        and isinstance(set_kg, str)
        and set_kg.strip()
        and mcp_kg.strip() != set_kg.strip()
    ):
        reasons.append(
            f"KG_COLLECTION {mcp_kg.strip()!r} (.mcp.json) != "
            f"{set_kg.strip()!r} (settings.json)"
        )

    # SHARED_KG_COLLECTION: empty/absent in .mcp.json while settings.json has a
    # non-empty value → shared-KG merge silently disabled (the Fabio symptom).
    set_shared = settings_env.get("SHARED_KG_COLLECTION")
    if isinstance(set_shared, str) and set_shared.strip():
        mcp_shared = mcp_env.get("SHARED_KG_COLLECTION")
        if isinstance(mcp_shared, str) and not mcp_shared.strip():
            # Present-but-empty shared: a standalone contradiction.
            reasons.append(
                "SHARED_KG_COLLECTION empty in .mcp.json while settings.json "
                f"has {set_shared.strip()!r} (shared-KG merge disabled)"
            )
        elif "SHARED_KG_COLLECTION" not in mcp_env and reasons:
            # Absent shared: the subprocess takes the WHOLE .mcp.json env (envs
            # are NOT merged key-by-key across the two files — the
            # higher-precedence .mcp.json dict wins), so a weaviate-kg env that
            # omits shared runs with NO shared merge. Only name it when the
            # block is ALREADY proven stale by url/collection; never let a
            # missing shared alone trigger the action.
            reasons.append(
                "SHARED_KG_COLLECTION absent from the .mcp.json weaviate-kg "
                f"env while settings.json has {set_shared.strip()!r} "
                "(shared-KG merge disabled under .mcp.json precedence)"
            )

    return reasons or None


def _find_matching_mcp_json_backup(
    project_root: Path,
    mcp_path: Path,
) -> "Optional[Path]":
    """v0.2.89 review MAJOR-4.2: restore-detection scan.

    Returns the first existing ``.mcp.json.bak-*`` sibling whose BYTES are
    identical to the current ``.mcp.json`` — evidence the user deliberately
    restored a quarantine backup — or ``None``. Scans BOTH backup locations:
    the current ``.claude/context/`` home (NIT-3) and the legacy project-root
    siblings written by earlier builds. Byte comparison (not text) so CRLF
    files round-trip exactly. Soft-fail per candidate: an unreadable backup
    simply doesn't match.
    """
    try:
        current = mcp_path.read_bytes()
    except OSError:
        return None
    scan_dirs = (project_root / MCP_JSON_BACKUP_REL, project_root)
    for scan_dir in scan_dirs:
        try:
            candidates = sorted(scan_dir.glob(".mcp.json.bak-*"))
        except OSError:
            continue
        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.read_bytes() == current:
                    return candidate
            except OSError:
                continue
    return None


def quarantine_stale_mcp_json_shadow(
    project_root: Path,
    *,
    log_event: Optional[Callable] = None,
    print_fn: Optional[Callable[[str], None]] = None,
):
    """Detect + quarantine an orphan `<project>/.mcp.json` weaviate-kg block
    that shadows the migrated `.claude/settings.json`.

    v0.2.89 FIX 2. Returns a ``DeferralEntry`` describing the quarantine when it
    acts (or an INFO entry when a deliberate restore is detected and honoured),
    or ``None`` when there is nothing to do / the block is consistent /
    evidence is insufficient (all the leave-alone legs). Single-project scope.
    Soft-fail throughout — any read/parse error or ambiguity leaves `.mcp.json`
    untouched and returns ``None``.

    Two owners share this ONE helper (v0.2.89 review MAJOR-1): install.py runs
    it for the orchestrator root (``_check_stale_mcp_json_shadow``), and the
    bundle engine (``vco_lib.project_init.install_project_bundle``) runs it for
    every NON-root project on ``install-bundle --update`` — which is how the
    launcher's Update-all and the CLI bundle-update reach user projects.

    Guard rails, in evaluation order:
      * :data:`MCP_JSON_QUARANTINE_SKIP_ENV` (``VCO_SKIP_MCP_JSON_QUARANTINE``)
        set truthy → total no-op (explicit user opt-out, MAJOR-4.3).
      * A ``.mcp.json`` byte-identical to ANY existing ``.mcp.json.bak-*``
        backup (new ``.claude/context/`` home or legacy project-root) means the
        user deliberately restored a prior quarantine backup → LEAVE ALONE and
        return an informational ``stale_mcp_json_restore_detected`` entry
        instead of re-quarantining (MAJOR-4.2 — no restore/quarantine
        ping-pong).

    Action, ONLY on a demonstrable contradiction (see
    :func:`mcp_json_weaviate_env_is_stale`):
      1. Back up the whole `.mcp.json` to
         ``.claude/context/.mcp.json.bak-<YYYY-MM-DD>`` (atomic write).
      2. Remove ONLY the ``weaviate-kg`` entry from ``mcpServers`` (so it
         inherits the correct env from settings.json) and rewrite `.mcp.json`
         ATOMICALLY (via :func:`vco_lib.atomic.atomic_write_text` — a crash
         mid-write leaves either the old file or the new one, never a torn
         file the next run's parse would choke on). Other MCP entries are
         semantically preserved (the file is re-serialized as JSON, so
         formatting/key-order may change but no entry's content does); an
         empty ``mcpServers`` object is a valid no-op.
      3. Return a ``stale_mcp_json_shadow_quarantined`` deferral entry.
    """
    import json
    import os

    def _log(step: str, phase: str, detail: str = "", data=None) -> None:
        if log_event is None:
            return
        try:
            log_event(step, phase, detail, data=data)
        except TypeError:  # logger without a data= kwarg
            log_event(step, phase, detail)

    # MAJOR-4.3: explicit user opt-out — checked HERE (the one shared helper)
    # so it covers every caller (install.py root check + bundle engine).
    skip_flag = os.environ.get(MCP_JSON_QUARANTINE_SKIP_ENV, "").strip().lower()
    if skip_flag in ("1", "true", "yes", "on"):
        _log(
            "mcp_json_shadow_check", "info",
            f"skipped: {MCP_JSON_QUARANTINE_SKIP_ENV} is set — "
            ".mcp.json left untouched by user opt-out",
        )
        return None

    mcp_path = project_root / ".mcp.json"
    if not mcp_path.is_file():
        return None  # (c) no .mcp.json → no-op
    try:
        raw = mcp_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        _log("mcp_json_shadow_check", "warn", f"could not read {mcp_path}: {exc}")
        return None
    try:
        if not isinstance(data, dict):
            return None
        mcp_servers = data.get("mcpServers")
        if not isinstance(mcp_servers, dict):
            return None
        weaviate_entry = mcp_servers.get("weaviate-kg")
        if not isinstance(weaviate_entry, dict):
            return None  # no weaviate-kg block to compare
        mcp_env = weaviate_entry.get("env")
        if not isinstance(mcp_env, dict):
            return None

        settings_env = resolve_settings_weaviate_env(project_root)
        if settings_env is None:
            _log(
                "mcp_json_shadow_check", "info",
                "found .mcp.json weaviate-kg env but no comparable "
                "settings.json env — leaving .mcp.json untouched",
            )
            return None

        reasons = mcp_json_weaviate_env_is_stale(mcp_env, settings_env)
        if not reasons:
            # (b) consistent .mcp.json → LEAVE UNTOUCHED (leave-alone leg).
            _log(
                "mcp_json_shadow_check", "info",
                ".mcp.json weaviate-kg env is consistent with settings.json — "
                "leaving it untouched",
            )
            return None

        # ── MAJOR-4.2: restore-detection guard. A `.mcp.json` byte-identical
        # to ANY existing quarantine backup means the user deliberately
        # restored it — honour the decision, LEAVE ALONE, and surface an
        # informational entry instead of re-quarantining forever (the
        # pre-fix behaviour was a restore/quarantine ping-pong with no
        # escape: the deferral said "restore the backup to roll back" while
        # every --update re-quarantined the restored file).
        restored_from = _find_matching_mcp_json_backup(project_root, mcp_path)
        if restored_from is not None:
            reasons_str = "; ".join(reasons)
            _log(
                "mcp_json_shadow_check", "info",
                "deliberate .mcp.json restore detected (byte-identical to "
                f"{restored_from}) — NOT re-quarantining",
                data={
                    "mcp_json": str(mcp_path),
                    "matched_backup": str(restored_from),
                    "reasons": reasons,
                },
            )
            from vco_lib.deferral_report import DeferralEntry

            return DeferralEntry(
                condition_id="stale_mcp_json_restore_detected",
                title=(
                    "Deliberate .mcp.json restore detected — weaviate-kg "
                    "block NOT re-quarantined"
                ),
                detected=(
                    f"`{mcp_path}` carries a `weaviate-kg` env block that "
                    f"contradicts the migrated `.claude/settings.json` "
                    f"({reasons_str}), but the file is byte-identical to a "
                    f"prior quarantine backup (`{restored_from}`) — you (or a "
                    "script) restored that backup on purpose, so VCO is "
                    "leaving it alone instead of re-quarantining it."
                ),
                why_deferred=(
                    "A byte-identical match against a quarantine backup is "
                    "treated as an explicit user decision to keep this "
                    ".mcp.json. VCO will not fight it — no restore/quarantine "
                    "ping-pong."
                ),
                command_to_apply=(
                    "# No action required if the restore was intentional.\n"
                    f"# To permanently silence this check: set "
                    f"{MCP_JSON_QUARANTINE_SKIP_ENV}=1 in the environment of "
                    "install.py / install-bundle runs.\n"
                    "# To let VCO quarantine the stale block again: delete "
                    "the matching backup\n"
                    f"#   {restored_from}\n"
                    "# (or edit .mcp.json so it no longer matches the backup "
                    "byte-for-byte)."
                ),
                severity="info",
                kg_node_refs=[
                    "knowledge/concepts/orchestrator-mcp-servers.md",
                ],
            )

        # ── Stale: back up, then quarantine ONLY the weaviate-kg block. ──
        # MAJOR-3: BOTH writes route through the shared atomic primitive
        # (`vco_lib.atomic.atomic_write_text` — tempfile + fsync +
        # os.replace) instead of `Path.write_text`'s truncate-then-write. A
        # crash mid-rewrite previously left a TORN `.mcp.json`, and the torn
        # file made the NEXT run's json parse fail → soft-fail no-op → the
        # torn file stayed torn forever. One-home rule: no bespoke
        # atomic-write copy here.
        from datetime import datetime, timezone

        from vco_lib.atomic import atomic_write_text

        # NIT-3: backups live under `.claude/context/` (out of the project
        # root's git-status noise). Restore-detection above still honours
        # legacy root-level `.mcp.json.bak-*` siblings from earlier builds.
        backup_dir = project_root / MCP_JSON_BACKUP_REL
        date_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        backup_path = backup_dir / f".mcp.json.bak-{date_stamp}"
        if backup_path.exists():
            n = 2
            while True:
                candidate = backup_dir / f".mcp.json.bak-{date_stamp}-{n}"
                if not candidate.exists():
                    backup_path = candidate
                    break
                n += 1
        try:
            atomic_write_text(backup_path, raw)
        except OSError as exc:
            _log(
                "mcp_json_shadow_check", "warn",
                f"could not write backup {backup_path}: {exc} — "
                "leaving .mcp.json untouched",
            )
            return None

        remaining = {
            name: entry
            for name, entry in mcp_servers.items()
            if name != "weaviate-kg"
        }
        data["mcpServers"] = remaining
        try:
            atomic_write_text(mcp_path, json.dumps(data, indent=2) + "\n")
        except OSError as exc:
            _log(
                "mcp_json_shadow_check", "warn",
                f"could not rewrite {mcp_path}: {exc} (backup at {backup_path})",
            )
            return None

        other_servers = sorted(remaining.keys())
        preserved_note = (
            f"Preserved {len(other_servers)} other MCP entr"
            f"{'y' if len(other_servers) == 1 else 'ies'} "
            f"({', '.join(f'`{s}`' for s in other_servers)})."
            if other_servers
            else "No other MCP entries were present; `mcpServers` is now empty."
        )
        reasons_str = "; ".join(reasons)
        if print_fn is not None:
            try:
                print_fn(
                    f"  Quarantined stale weaviate-kg env from {mcp_path} "
                    f"(shadowed settings.json). Backup: {backup_path}"
                )
            except Exception:  # noqa: BLE001
                pass
        _log(
            "mcp_json_shadow_check", "ok",
            "quarantined stale .mcp.json weaviate-kg env block",
            data={
                "mcp_json": str(mcp_path),
                "backup": str(backup_path),
                "reasons": reasons,
                "preserved": other_servers,
            },
        )

        from vco_lib.deferral_report import DeferralEntry

        return DeferralEntry(
            condition_id="stale_mcp_json_shadow_quarantined",
            title=(
                "Quarantined stale .mcp.json weaviate-kg env "
                "(shadowed migrated settings.json)"
            ),
            detected=(
                f"`{mcp_path}` carried a `weaviate-kg` env block that "
                f"contradicted the migrated `.claude/settings.json`: "
                f"{reasons_str}. `.mcp.json` takes PRECEDENCE over settings.json "
                "for MCP env, so every KG search ran against the stale "
                "endpoint/collection (and cross-project shared-KG merge was "
                "silently OFF). The weaviate-kg env block was removed so "
                f"weaviate-kg inherits the correct env from settings.json. "
                f"{preserved_note}"
            ),
            why_deferred=(
                "The stale block was auto-quarantined (backed up first), but "
                "VCO does NOT manage `.mcp.json` (it is Anthropic's "
                "project-scoped config), so this deferral records the change "
                "for the user's review rather than silently mutating an "
                "un-owned file with no trace. Restart Claude Code so the "
                "weaviate-kg MCP re-reads settings.json."
            ),
            command_to_apply=(
                "# No action required — the stale weaviate-kg block was already "
                "removed.\n"
                f"# Backup of the original: {backup_path}\n"
                "# Restart Claude Code so weaviate-kg picks up the correct env "
                "from .claude/settings.json.\n"
                f"# To roll back: restore the backup over {mcp_path} — VCO "
                "detects a byte-identical\n"
                "# restore and will NOT re-quarantine it (no ping-pong).\n"
                f"# To disable this check entirely: set "
                f"{MCP_JSON_QUARANTINE_SKIP_ENV}=1."
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/orchestrator-mcp-servers.md",
            ],
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail
        _log("mcp_json_shadow_check", "warn", f"could not check .mcp.json shadow: {exc}")
        return None
