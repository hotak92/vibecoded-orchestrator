# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Bundled-knowledge residue cleanup (v0.2.89 §7 — Fabio wave-2).

Why this module exists
----------------------
Pre-v0.2.81 the curated bundled KG set (~115 nodes) was materialized into
EVERY project's ``knowledge/`` + embedded into every project's KG collection.
v0.2.81 made the set root-only, and the ``knowledge-retired`` manifest branch
(project_init.py orphan loop) prunes the MANIFEST entries on the first
post-.81 bundle update — deliberately leaving the files on disk and the
Weaviate rows + vectors in place. On field installs (Fabio, v0.2.88) that
residue is now unreachable through any manifest-keyed mechanism: the manifest
no longer lists the files.

This module is the scan-based, manifest-independent cleanup: a file under a
NON-root project's ``knowledge/`` is removed ONLY when its
``(rel path, content signature)`` pair matches the shipped provenance
registry ``templates/knowledge/.curated_hashes.json`` — i.e. it is
byte-equivalent (modulo the machine-written ``updated:`` line) to a version
VCO itself shipped, and the identical content remains available to the
project via the unconditional shared-KG read fan-out.

Data-safety invariants (binding, from PLAN-fabio-wave2-2026-07-30 §7.2):

* Gates, in order — any miss ⇒ skip with one log line:
  1. NOT a root bundle target (the root's ``knowledge/`` IS the canonical
     materialization of the shared collection).
  2. The project has NOT opted out of shared reads
     (``SHARED_KG_READ_DISABLED != true`` — an accept-loss project's on-disk
     copies are its ONLY curated access).
  3. Registry present + parseable.
  4. Weaviate reachable (bounded probe). If down: NO deletion this run +
     a ``residue_cleanup_pending`` deferral ("will retry next bundle
     update").
* Ordering rule: **Weaviate rows first, file second** — a deleted file with
  surviving rows is an orphan embedding (invisible, permanent); a surviving
  file with deleted rows self-heals on the next kg-sync.
* Rel-path + signature must BOTH match: a byte-identical copy the user
  deliberately relocated is user-curated placement — left alone.
* ``\\`` → ``/`` normalization on every compared rel path (the v0.2.81 B1
  Windows-separator lesson).
* Top-level ``_PER_PROJECT_KNOWLEDGE_FILES`` allowlist names (TAG_HIERARCHY,
  VOCABULARY, …) are excluded — they legitimately ship per-project.
* Only the four curated subdirs (``concepts models patterns tools``) are
  pruned when left empty.

The step is wired into ``install_project_bundle`` behind a soft-fail wrapper
— it must NEVER fail the bundle update.

Signature note (one-home + parity): :func:`content_signature_excluding_updated`
is the vco_lib home of the storage-layer signature. It MUST match
``templates/scripts/sync_knowledge_graph.py::_content_signature_excluding_updated``
byte-for-byte in behavior — a parity test
(``tests/test_v0289_curated_hash_registry.py``) locks the two together. The
sync script cannot be imported here (module-level side effects: hub
resolution, env reads), which is why a mirrored implementation + parity lock
is used instead of an import.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from vco_lib import weaviate_helpers as _wh
from vco_lib.hashing import sha256_text

#: The four curated subdirectories the bundled set ships under. Only these
#: are pruned when emptied by the cleanup — never user-created subdirs.
CURATED_SUBDIRS: tuple[str, ...] = ("concepts", "models", "patterns", "tools")

#: Basename of the shipped provenance registry (lives in
#: ``templates/knowledge/`` next to the curated nodes; generated at release
#: time by ``scripts/build_curated_hash_registry.py``).
REGISTRY_BASENAME = ".curated_hashes.json"

#: Deferral condition IDs owned by this module.
CID_REMOVED = "residue_cleanup_removed"
CID_PENDING = "residue_cleanup_pending"

#: How many removed paths the info deferral lists before truncating.
_DEFERRAL_PATHS_SHOWN = 10


# ---------------------------------------------------------------------------
# Storage-layer content signature (parity-locked with sync_knowledge_graph.py)
# ---------------------------------------------------------------------------

def content_signature_excluding_updated(content: str) -> str:
    """SHA256 of the node content excluding the frontmatter ``updated:`` line.

    vco_lib home of the STORAGE-LAYER signature — mirrors
    ``sync_knowledge_graph.py::_content_signature_excluding_updated`` exactly
    (parity test pins the behavior; see module docstring). Deliberately
    tolerant ONLY of ``updated:``-line churn, which is machine-written; any
    body or other-frontmatter edit changes the signature.
    """
    if not content.strip().startswith("---"):
        return sha256_text(content)
    parts = content.split("---", 2)
    if len(parts) < 3:
        return sha256_text(content)
    fm_text = parts[1]
    body = parts[2]
    fm_no_updated = re.sub(r"^updated:.*$\n?", "", fm_text, flags=re.MULTILINE)
    return sha256_text(fm_no_updated + body)


# ---------------------------------------------------------------------------
# Small shared helpers (also consumed by vco_lib.collection_repair)
# ---------------------------------------------------------------------------

def _http_request(
    method: str,
    url: str,
    *,
    body: Optional[dict] = None,
    timeout: float = 30.0,
) -> tuple[int, bytes]:
    """Module-level delegator to :func:`vco_lib.weaviate_helpers.http_request`.

    Exists so tests can ``mock.patch.object(knowledge_residue,
    "_http_request", ...)`` — same seam convention as
    ``project_init._http_request``.
    """
    return _wh.http_request(method, url, body=body, timeout=timeout)


def weaviate_reachable(weaviate_url: str, *, timeout: float = 4.0) -> bool:
    """Bounded probe of ``/v1/.well-known/ready``. True only on HTTP 200."""
    base = (weaviate_url or "").rstrip("/")
    if not base:
        return False
    try:
        status, _ = _http_request(
            "GET", f"{base}/v1/.well-known/ready", timeout=timeout,
        )
        return status == 200
    except Exception:  # noqa: BLE001 — probe is best-effort by contract
        return False


def project_settings_env(folder: Path) -> dict:
    """Return the ``env`` dict from ``<folder>/.claude/settings.json``.

    Soft-fail: ``{}`` on a missing / unparseable file or a non-dict ``env``
    key. This is the canonical per-project env surface (CLAUDE.md: the
    channel that propagates to MCP subprocesses on every Claude Code
    surface).
    """
    try:
        settings_file = Path(folder) / ".claude" / "settings.json"
        if not settings_file.is_file():
            return {}
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        env = data.get("env") if isinstance(data, dict) else None
        return env if isinstance(env, dict) else {}
    except Exception:  # noqa: BLE001 — settings read is best-effort
        return {}


_TRUTHY = frozenset({"1", "true", "yes"})


def shared_read_disabled_for(folder: Path) -> bool:
    """Resolve the project's ``SHARED_KG_READ_DISABLED`` gate.

    Sources, in order: ``.claude/settings.json`` ``env`` block, then the
    shell-sourced ``.claude/env``. Absent everywhere ⇒ False (shared reads
    are unconditional by default).
    """
    env = project_settings_env(folder)
    val = env.get("SHARED_KG_READ_DISABLED")
    if isinstance(val, str) and val.strip():
        return val.strip().lower() in _TRUTHY
    # Fallback: the shell-sourced .claude/env (export KEY="value" lines).
    try:
        env_file = Path(folder) / ".claude" / "env"
        if env_file.is_file():
            text = env_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(
                r'^\s*(?:export\s+)?SHARED_KG_READ_DISABLED=["\']?([^"\'\s]+)',
                text,
                flags=re.MULTILINE,
            )
            if m:
                return m.group(1).strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001 — env read is best-effort
        pass
    return False


def load_curated_registry(registry_path: Path) -> Optional[dict[str, set]]:
    """Load + validate the provenance registry.

    Returns ``{rel_posix: {sig, ...}}`` or ``None`` when the file is missing,
    unparseable, or not schema_version 1 (gate 3 — the caller skips).
    Registry keys are separator-normalized (``\\`` → ``/``) so a registry
    generated on any OS matches POSIX-shaped scan rels.
    """
    try:
        raw = json.loads(Path(registry_path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            return None
        files = raw.get("files")
        if not isinstance(files, dict):
            return None
        out: dict[str, set] = {}
        for rel, sigs in files.items():
            if not isinstance(rel, str) or not isinstance(sigs, list):
                continue
            norm = rel.replace("\\", "/")
            out.setdefault(norm, set()).update(
                s for s in sigs if isinstance(s, str) and s
            )
        return out
    except Exception:  # noqa: BLE001 — unparseable registry ⇒ gate miss
        return None


def _default_orchestrator_root() -> Path:
    """The orchestrator clone this vco_lib belongs to (``vco_lib/..``)."""
    return Path(__file__).resolve().parent.parent


def _log_line(log_event: Optional[Callable[..., None]], phase: str,
              detail: str, *, data: Any = None) -> None:
    """One-line logging shim compatible with install.py's ``_log_install_event``."""
    if log_event is None:
        return
    try:
        log_event("4.bundle.residue", phase, detail, data=data)
    except TypeError:
        try:
            log_event("4.bundle.residue", phase, detail)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — logging must never break the cleanup
        pass


class _RowDeleteHTTPError(RuntimeError):
    """Weaviate answered the batch delete with a non-200 (server alive —
    per-file failure, NOT a transport abort)."""


def _delete_rows_by_file_path(
    weaviate_url: str, collection: str, file_path: str, *, timeout: float = 30.0,
) -> int:
    """Delete every ``collection`` row whose ``file_path`` equals *file_path*.

    Uses the REST ``DELETE /v1/batch/objects`` where-filter form (removes
    vectors with the objects). Returns the number of successfully deleted
    rows. Raises :class:`_RowDeleteHTTPError` on a non-200 answer; transport
    errors (connection refused / timeout) propagate as-is so the caller can
    distinguish "server alive but refused" from "server gone".
    """
    base = weaviate_url.rstrip("/")
    status, body = _http_request(
        "DELETE",
        f"{base}/v1/batch/objects",
        body={
            "match": {
                "class": collection,
                "where": {
                    "path": ["file_path"],
                    "operator": "Equal",
                    "valueText": file_path,
                },
            },
            "output": "minimal",
            "dryRun": False,
        },
        timeout=timeout,
    )
    if status != 200:
        raise _RowDeleteHTTPError(
            f"DELETE /v1/batch/objects ({collection}, {file_path!r}) → "
            f"HTTP {status}: {body[:200]!r}"
        )
    try:
        payload = json.loads(body.decode("utf-8"))
        results = payload.get("results") or {}
        n = results.get("successful", 0)
        return int(n) if isinstance(n, (int, float)) else 0
    except Exception:  # noqa: BLE001 — a 200 with an odd body still deleted
        return 0


def _row_path_shapes(rel_posix: str) -> list[str]:
    """Both stored ``file_path`` shapes for a knowledge rel path.

    Rows written on POSIX carry ``knowledge/concepts/foo.md``; rows written
    on Windows carry ``knowledge\\concepts\\foo.md`` (``str(Path(...))`` is
    host-OS-shaped — the v0.2.81 separator lesson). Both must be deleted.
    """
    posix = f"knowledge/{rel_posix}"
    windows = posix.replace("/", "\\")
    return [posix] if windows == posix else [posix, windows]


def _iter_residue_candidates(
    knowledge_root: Path,
    registry: dict[str, set],
    allowlist: frozenset,
) -> Iterable[tuple[str, Path]]:
    """Yield ``(rel_posix, abs_path)`` for provably-bundled unmodified files.

    A file is a candidate ONLY when its separator-normalized rel path within
    ``knowledge/`` AND its content signature BOTH match the registry, and it
    is not a top-level allowlisted per-project file.
    """
    if not knowledge_root.is_dir():
        return
    for f in sorted(knowledge_root.rglob("*.md")):
        if not f.is_file():
            continue
        try:
            rel_parts = f.relative_to(knowledge_root).parts
        except ValueError:  # pragma: no cover — rglob guarantees containment
            continue
        # Allowlist gate mirrors project_init._enumerate_knowledge_ops: the
        # per-project files live at depth 1 — exclude exactly those.
        if len(rel_parts) == 1 and rel_parts[0] in allowlist:
            continue
        rel_posix = "/".join(rel_parts).replace("\\", "/")
        sigs = registry.get(rel_posix)
        if not sigs:
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # unreadable ⇒ leave alone (default to safety)
        if content_signature_excluding_updated(content) in sigs:
            yield rel_posix, f


def _prune_empty_curated_subdirs(knowledge_root: Path) -> list[str]:
    """Remove now-empty dirs under the four curated subdirs (bottom-up),
    then the subdirs themselves when empty. Never touches other dirs."""
    pruned: list[str] = []
    for name in CURATED_SUBDIRS:
        base = knowledge_root / name
        if not base.is_dir():
            continue
        # Bottom-up: deepest first so nested empties collapse upward.
        subdirs = sorted(
            (d for d in base.rglob("*") if d.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for d in [*subdirs, base]:
            try:
                next(d.iterdir())
            except StopIteration:
                try:
                    d.rmdir()
                    pruned.append(str(d.relative_to(knowledge_root.parent)))
                except OSError:
                    pass
            except OSError:
                pass
    return pruned


# ---------------------------------------------------------------------------
# The cleanup step
# ---------------------------------------------------------------------------

def cleanup_bundled_knowledge_residue(
    folder: Path,
    weaviate_url: str,
    kg_collection: str,
    *,
    dry_run: bool = False,
    orchestrator_root: Optional[Path] = None,
    is_root_target: Optional[bool] = None,
    shared_read_disabled: Optional[bool] = None,
    registry_path: Optional[Path] = None,
    log_event: Optional[Callable[..., None]] = None,
) -> dict:
    """Remove provably-bundled, unmodified curated-knowledge residue.

    See the module docstring for the full data-safety contract. Returns a
    JSON-serialisable dict::

        {
          "skipped": None | "<gate reason>",
          "dry_run": bool,
          "candidates": [<rel>...],       # (rel, sig)-matched files found
          "removed": [<rel>...],          # files actually deleted (rows first)
          "rows_deleted": int,            # Weaviate rows removed (with vectors)
          "pruned_dirs": [<rel>...],
          "pending": bool,                # Weaviate down / lost mid-run
          "errors": [str...],
        }

    Never raises for per-file failures; the caller additionally wraps the
    whole call in a soft-fail guard (the step must never fail the bundle
    update).
    """
    folder = Path(folder).resolve()
    result: dict = {
        "skipped": None,
        "dry_run": bool(dry_run),
        "candidates": [],
        "removed": [],
        "rows_deleted": 0,
        "pruned_dirs": [],
        "pending": False,
        "errors": [],
    }

    orch_root = (
        Path(orchestrator_root).resolve()
        if orchestrator_root is not None
        else _default_orchestrator_root()
    )

    # Gate 1 — never on a root target (the root's knowledge/ IS the canonical
    # materialization; deleting there destroys the shared seed).
    if is_root_target is None:
        from vco_lib.project_init import _is_root_bundle_target
        is_root_target = _is_root_bundle_target(orch_root, folder)
    if is_root_target:
        result["skipped"] = "root target"
        _log_line(log_event, "ok",
                  "residue cleanup skipped: root bundle target")
        return result

    # Gate 2 — accept-loss projects keep their on-disk copies (their ONLY
    # curated access per the v0.2.81 contract).
    if shared_read_disabled is None:
        shared_read_disabled = shared_read_disabled_for(folder)
    if shared_read_disabled:
        result["skipped"] = "shared-read disabled"
        _log_line(log_event, "ok",
                  "residue cleanup skipped: SHARED_KG_READ_DISABLED=true "
                  "(on-disk copies are this project's only curated access)")
        return result

    # Gate 3 — registry present + parseable.
    reg_path = (
        Path(registry_path)
        if registry_path is not None
        else orch_root / "templates" / "knowledge" / REGISTRY_BASENAME
    )
    registry = load_curated_registry(reg_path)
    if registry is None:
        result["skipped"] = "registry missing or unparseable"
        _log_line(log_event, "warn",
                  f"residue cleanup skipped: registry missing/unparseable "
                  f"at {reg_path}")
        return result

    if not kg_collection or not str(kg_collection).strip():
        # Without the collection name the rows-first ordering cannot be
        # honored — deleting files anyway would orphan the embeddings.
        result["skipped"] = "no kg_collection resolved"
        _log_line(log_event, "warn",
                  "residue cleanup skipped: no KG collection name resolved")
        return result
    kg_collection = str(kg_collection).strip()

    # Disk-only scan (no destructive action yet). Doing this before the
    # reachability gate lets a residue-free project skip the pending
    # deferral entirely — a "cleanup pending" entry for a project with zero
    # residue would be semantically false. Deviation from the literal §7.2
    # gate order is scan-placement only: NO deletion ever happens without
    # the probe below passing.
    from vco_lib.project_init import _PER_PROJECT_KNOWLEDGE_FILES
    knowledge_root = folder / "knowledge"
    candidates = list(_iter_residue_candidates(
        knowledge_root, registry, _PER_PROJECT_KNOWLEDGE_FILES,
    ))
    result["candidates"] = [rel for rel, _ in candidates]

    if not candidates:
        _log_line(log_event, "ok", "residue cleanup: no residue found")
        if not dry_run:
            from vco_lib import deferral_emit as _de
            _de.resolve_conditions(folder, [CID_PENDING])
        return result

    # Gate 4 — Weaviate reachable (bounded probe). Rows must go first; a
    # down Weaviate means NO deletion this run.
    if not weaviate_reachable(weaviate_url):
        result["pending"] = True
        _log_line(log_event, "warn",
                  f"residue cleanup deferred: weaviate unreachable at "
                  f"{weaviate_url} ({len(candidates)} candidate(s) intact)")
        if not dry_run:
            _emit_pending_deferral(
                folder, len(candidates), weaviate_url, log_event,
            )
        return result

    if dry_run:
        _log_line(log_event, "ok",
                  f"residue cleanup dry-run: {len(candidates)} file(s) "
                  "would be removed (rows first, then file)")
        return result

    removed: list[str] = []
    rows_deleted = 0
    transport_lost = False
    for rel, abs_path in candidates:
        if transport_lost:
            break
        # Rows FIRST (both separator shapes), file second.
        row_delete_ok = True
        for fp_shape in _row_path_shapes(rel):
            try:
                rows_deleted += _delete_rows_by_file_path(
                    weaviate_url, kg_collection, fp_shape,
                )
            except _RowDeleteHTTPError as exc:
                # Server alive but refused — per-file failure: keep the
                # file (a file without rows-first completion must survive).
                result["errors"].append(str(exc))
                row_delete_ok = False
                break
            except Exception as exc:  # noqa: BLE001 — transport gone mid-run
                result["errors"].append(
                    f"weaviate transport lost mid-run: {exc}"
                )
                row_delete_ok = False
                transport_lost = True
                break
        if not row_delete_ok:
            continue
        try:
            abs_path.unlink()
            removed.append(rel)
        except OSError as exc:
            # Rows already gone, file survives — self-heals on the next
            # kg-sync (the benign side of the ordering asymmetry).
            result["errors"].append(f"could not delete {rel}: {exc}")

    result["removed"] = removed
    result["rows_deleted"] = rows_deleted
    result["pending"] = transport_lost

    if removed:
        result["pruned_dirs"] = _prune_empty_curated_subdirs(knowledge_root)

    from vco_lib import deferral_emit as _de
    if removed:
        _emit_removed_deferral(
            folder, removed, kg_collection, log_event,
        )
    if transport_lost:
        remaining = len(candidates) - len(removed)
        _emit_pending_deferral(folder, remaining, weaviate_url, log_event)
    else:
        _de.resolve_conditions(folder, [CID_PENDING])

    _log_line(
        log_event, "ok",
        f"residue cleanup: removed {len(removed)} file(s), "
        f"{rows_deleted} Weaviate row(s) from {kg_collection!r}",
        data={"removed": len(removed), "rows_deleted": rows_deleted},
    )
    return result


def _emit_removed_deferral(
    folder: Path,
    removed: list,
    kg_collection: str,
    log_event: Optional[Callable[..., None]],
) -> None:
    """ONE ``residue_cleanup_removed`` info entry (count + first N paths)."""
    from vco_lib import deferral_emit as _de
    from vco_lib.deferral_report import DeferralEntry

    shown = removed[:_DEFERRAL_PATHS_SHOWN]
    lines = "\n".join(f"- `knowledge/{rel}`" for rel in shown)
    if len(removed) > len(shown):
        lines += f"\n- … and {len(removed) - len(shown)} more"
    entry = DeferralEntry(
        condition_id=CID_REMOVED,
        title="Bundled curated knowledge residue removed",
        detected=(
            f"{len(removed)} bundled curated knowledge file(s) — "
            f"byte-identical (modulo the machine-written `updated:` line) to "
            f"a version VCO itself shipped — were removed from `knowledge/`, "
            f"together with their rows and vectors in `{kg_collection}`:\n"
            f"{lines}"
        ),
        why_deferred=(
            "Informational — no action required. Since v0.2.81 the curated "
            "set lives root-only and this content is served by the shared "
            "KG collection; the on-disk copies were pre-v0.2.81 residue. "
            "Modified, relocated, or unrecognized files were left untouched."
        ),
        command_to_apply=(
            "# Informational. Dismiss when read:\n"
            f"python -m vco_lib.project_init dismiss-deferral "
            f"--folder {str(folder)!r} --condition-id {CID_REMOVED}"
        ),
        severity="info",
    )
    _de.emit(folder, entry, log=None)
    _log_line(log_event, "ok",
              f"residue_cleanup_removed deferral written "
              f"({len(removed)} file(s))")


def _emit_pending_deferral(
    folder: Path,
    candidate_count: int,
    weaviate_url: str,
    log_event: Optional[Callable[..., None]],
) -> None:
    """``residue_cleanup_pending`` — Weaviate down; nothing was deleted."""
    from vco_lib import deferral_emit as _de
    from vco_lib.deferral_report import DeferralEntry

    entry = DeferralEntry(
        condition_id=CID_PENDING,
        title="Bundled knowledge residue cleanup pending (Weaviate unreachable)",
        detected=(
            f"{candidate_count} bundled curated residue file(s) were "
            f"identified under `knowledge/` but Weaviate was unreachable at "
            f"{weaviate_url}; nothing was deleted this run."
        ),
        why_deferred=(
            "Rows must be removed BEFORE the on-disk file — deleting the "
            "file first would leave orphaned embeddings (invisible, "
            "permanent). The cleanup retries automatically on the next "
            "bundle update."
        ),
        command_to_apply=(
            "# Start Weaviate, then re-run the bundle update "
            "(launcher: project Settings → Update bundle), or:\n"
            f"python -m vco_lib.project_init install-bundle "
            f"--folder {str(folder)!r} --update --json"
        ),
        severity="info",
    )
    _de.emit(folder, entry, log=None)
    _log_line(log_event, "warn",
              f"residue_cleanup_pending deferral written "
              f"({candidate_count} candidate(s))")
