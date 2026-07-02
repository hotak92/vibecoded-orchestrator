# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Revision-gated code-graph resync trigger (v0.2.72 P7).

Background
----------
P3 (model-aware chunking, ``templates/scripts/analyze_code_graph.py``) changed
how over-budget Function/Class entities are embedded: instead of truncating the
tail, they are now split into N chunks (``chunk_num``/``total_chunks``). That
invalidates the existing single-object embeddings of the ~7-9% of entities that
were over budget — their on-disk body text is unchanged, so the analyzer's
per-object content-hash tombstone-skip would skip them forever and they'd never
gain their chunks.

The analyzer stamps a per-object ``embed_revision`` property equal to
``CODEGRAPH_EMBED_REVISION``. On the next analyze it FORCES a re-embed of any
row whose stored ``embed_revision`` differs (or is NULL) — bypassing the
content-hash skip for exactly the stale rows, leaving everything already at the
current revision untouched.

This module is the *trigger* side: it kicks off a BACKGROUND, per-project,
resumable re-analyze so the revision-gated resync actually runs after an
``install.py --update`` — WITHOUT blocking the update. The heavy lifting (the
gate itself) lives in the analyzer; this module only decides *whether* and *how*
to launch it, and degrades gracefully when the code-embed service is down.

Design invariants (project rules)
---------------------------------
* **No global/process timeout.** A slow machine must be able to finish. The
  analyzer's per-embed-request guard (``VCT_EMBED_REQUEST_TIMEOUT_SECS``) is the
  correct granularity for catching a wedged embedder; we never impose a
  wall-clock deadline on the whole resync.
* **Background + non-blocking.** We ``Popen`` the analyzer detached and return
  immediately. The caller (``install.py --update``) does not wait on it.
* **Degrade, don't fail.** If the code-embed service (:11440) is unreachable,
  we DO NOT launch (a re-embed would fail per-object) — instead we return a
  ``deferred`` status and hand the caller a :class:`DeferralEntry` so the user
  is told to re-run once the service is up. The update itself still succeeds.
* **Resumable + idempotent.** Because the gate is per-object, an interrupted
  resync simply continues on the next run; re-running after completion is a
  cheap no-op (every row already at the current revision → all skip).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # DeferralEntry is optional at import time (unit tests may not need it)
    from vco_lib.deferral_report import DeferralEntry
except Exception:  # noqa: BLE001 — keep the module importable in isolation
    DeferralEntry = None  # type: ignore[assignment,misc]


logger = logging.getLogger(__name__)

DEFAULT_CODE_EMBED_PORT = 11440
_CONDITION_ID = "codegraph_embed_resync_pending"

# ── F9 (pre-gate audit): one-time prune of already-indexed ignore-set rows ──
#
# The P5 walker/dispatch exclusions stop NEW `.wt/` + vendor-bundle rows, but
# rows indexed BEFORE those exclusions shipped still pollute live collections
# (live-confirmed: worktree copies of tests injecting as retrieval context).
# The prune below deletes rows whose stored path falls in the CURRENT ignore
# set. This is DERIVED, regenerable data (the analyzer re-creates any row that
# genuinely belongs), so auto-applying is safe per the v0.2.60 regenerated-data
# precedent — counts are logged, everything soft-fails.

_CODEGRAPH_BASES: tuple = (
    "CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction",
)

# Path-part ignore set. MUST MATCH templates/scripts/analyze_code_graph.py::
# _COMMON_IGNORE_DIRS (+ `vendor`, which the analyzer applies via the js/ts/
# go/ruby language extras — pruning it unconditionally here matches the
# single-file dispatch's conservative `.wt`/`vendor` gate). `.claude` is added
# only when the caller says index_dot_claude=False for the project (the
# orchestrator root indexes .claude/ as first-party source — never prune it
# there).
_PRUNE_IGNORE_PARTS: frozenset = frozenset({
    '.git', '.svn', '.hg',
    '.venv', 'venv', 'env', '.env', 'virtualenv', '.tox', 'site-packages',
    '__pycache__', '.pytest_cache',
    'build', 'dist', 'out',
    'node_modules',
    'worktrees', '.wt',
    '.svelte-kit', '.next', '.nuxt', '.cache', '.parcel-cache', '.turbo',
    '.angular',
    'vendor',
})

# Filename skip suffixes. MUST MATCH the union of analyze_code_graph.py::
# _JS_SKIP_SUFFIXES + _TS_SKIP_SUFFIXES (build output / config / type stubs).
_PRUNE_SKIP_SUFFIXES: tuple = (
    '.min.js', '.bundle.js', '.chunk.js', '.config.js', '.config.mjs',
    '.d.ts', '.bundle.ts', '.chunk.ts', '.config.ts', '.config.mts',
)

# Per-base property carrying the source path. MUST MATCH the analyzer's
# storage shape (CodeModule keys on `path`; the rest on `file_path`).
_PRUNE_PATH_PROP: dict = {
    "CodeModule": "path",
}


def _collection_prefix(project_name: str) -> Optional[str]:
    """Project name → Weaviate class prefix, via the ENDORSED vco_lib wrapper
    (``codegraph_to_mermaid._sanitize_collection_prefix`` → SSOT
    ``project_naming.canonical_class_prefix``). No 5th sanitizer copy here —
    tests/test_canonical_class_prefix_parity.py guards against that.

    Returns ``None`` when the wrapper is unimportable or the name is unusable
    — the caller then does NOTHING (conservative default: never guess a
    prefix and delete from the wrong collections).
    """
    try:
        from vco_lib.codegraph_to_mermaid import (
            _sanitize_collection_prefix as _sanitize,
        )
    except Exception as exc:  # noqa: BLE001 — script-mode / partial install
        logger.warning("codegraph prune: prefix resolver unavailable: %s", exc)
        return None
    try:
        prefix = _sanitize(project_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("codegraph prune: cannot derive prefix: %s", exc)
        return None
    return prefix or None


def _path_is_ignored(file_path: str, *, index_dot_claude: bool = True) -> bool:
    """True when a stored row path falls in the CURRENT ignore set.

    Path-PART match (not substring) for directories — `my_vendor_tools/x.py`
    is NOT pruned; `vendor/x.py` is. Suffix match for build-output filenames.
    """
    if not file_path:
        return False
    norm = str(file_path).replace("\\", "/")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return False
    ignore = _PRUNE_IGNORE_PARTS
    if not index_dot_claude:
        ignore = ignore | frozenset({'.claude'})
    if any(p in ignore for p in parts[:-1]):
        return True
    name = parts[-1]
    if name.startswith('vite.config'):
        return True
    return any(name.endswith(s) for s in _PRUNE_SKIP_SUFFIXES)


def prune_ignored_rows(
    project_name: str,
    *,
    client=None,
    index_dot_claude: bool = True,
    weaviate_url: Optional[str] = None,
    grpc_port: Optional[int] = None,
) -> dict:
    """Delete code-graph rows whose stored path is in the CURRENT ignore set.

    Scoped STRICTLY to the project's own collections (``<Prefix>_CodeFunction``
    etc. — the per-project prefix is the tenant boundary). Returns a
    ``{collection_name: deleted_count}`` dict; every per-collection failure is
    logged and skipped (soft-fail — a prune failure must never fail the caller).

    ``client`` may be injected (tests); otherwise a connection is built from
    ``weaviate_url`` / ``$WEAVIATE_URL`` / localhost:8081 and closed on exit.
    """
    counts: dict = {}
    if not project_name:
        return counts

    own_client = False
    if client is None:
        try:
            import weaviate  # local import — soft-fail when not installed

            url = weaviate_url or os.environ.get("WEAVIATE_URL") or "http://localhost:8081"
            m = re.match(r"^https?://([^:/]+)(?::(\d+))?", url)
            host = m.group(1) if m else "localhost"
            http_port = int(m.group(2)) if (m and m.group(2)) else 8081
            gport = int(grpc_port or os.environ.get("GRPC_PORT") or 50052)
            client = weaviate.connect_to_custom(
                http_host=host, http_port=http_port, http_secure=False,
                grpc_host=host, grpc_port=gport, grpc_secure=False,
            )
            own_client = True
        except Exception as exc:  # noqa: BLE001 — no Weaviate → no prune
            logger.warning("codegraph prune: Weaviate unavailable: %s", exc)
            return counts

    try:
        try:
            from weaviate.classes.query import Filter
        except Exception as exc:  # noqa: BLE001
            logger.warning("codegraph prune: weaviate Filter unavailable: %s", exc)
            return counts

        prefix = _collection_prefix(project_name)
        if prefix is None:
            # Conservative default: no positive prefix confirmation → do
            # nothing rather than guess a delete target.
            return counts
        for base in _CODEGRAPH_BASES:
            coll_name = f"{prefix}_{base}"
            try:
                if hasattr(client.collections, "exists") and not client.collections.exists(coll_name):
                    continue
                coll = client.collections.get(coll_name)
                path_prop = _PRUNE_PATH_PROP.get(base, "file_path")
                to_delete: list = []
                for obj in coll.iterator(return_properties=[path_prop]):
                    p = getattr(obj, "properties", None) or {}
                    fp = p.get(path_prop) or ""
                    if _path_is_ignored(fp, index_dot_claude=index_dot_claude):
                        to_delete.append(obj.uuid)
                deleted = 0
                for i in range(0, len(to_delete), 100):
                    batch = to_delete[i:i + 100]
                    coll.data.delete_many(
                        where=Filter.by_id().contains_any(batch)
                    )
                    deleted += len(batch)
                if deleted:
                    logger.info(
                        "codegraph prune: %s — deleted %d ignore-set row(s)",
                        coll_name, deleted,
                    )
                counts[coll_name] = deleted
            except Exception as exc:  # noqa: BLE001 — per-collection soft-fail
                logger.warning("codegraph prune: %s failed: %s", coll_name, exc)
        return counts
    finally:
        if own_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


@dataclass
class ResyncTriggerResult:
    """Outcome of a resync-trigger attempt. Never raises; the caller inspects
    ``status`` to decide whether to record a deferral.

    ``status`` is one of:
      * ``"launched"``   — a background analyze was spawned (``pid`` is set).
      * ``"deferred"``   — the code-embed service was down; ``deferral`` carries
                            a :class:`DeferralEntry` (when the type is available)
                            for the caller to record. Nothing was spawned.
      * ``"skipped"``    — a precondition wasn't met (analyzer/python missing,
                            no project name). Soft no-op; ``message`` explains.
    """

    status: str
    message: str = ""
    pid: Optional[int] = None
    deferral: Optional[object] = None


def code_embed_service_healthy(
    code_embed_url: Optional[str] = None,
    *,
    timeout: float = 2.0,
) -> bool:
    """Return True iff the code-embedding service answers ``/health`` < 400.

    Resolution order for the base URL: explicit arg → ``CODE_EMBED_SERVICE_URL``
    env → ``http://localhost:<CODE_EMBED_PORT|11440>``. Never raises — any
    failure (connection refused, timeout, DNS) returns False so the caller
    degrades to the deferral path rather than crashing the update.
    """
    base = (
        code_embed_url
        or os.environ.get("CODE_EMBED_SERVICE_URL")
        or f"http://localhost:{os.environ.get('CODE_EMBED_PORT', DEFAULT_CODE_EMBED_PORT)}"
    )
    base = base.rstrip("/")
    health = base if base.endswith("/health") else f"{base}/health"
    try:
        resp = urllib.request.urlopen(health, timeout=timeout)
        return resp.status < 400
    except Exception:  # noqa: BLE001 — unreachable service → not healthy
        return False


def _resolve_analyzer(repo_root: Path) -> Optional[Path]:
    """Locate ``analyze_code_graph.py``. Prefers the shipped project copy under
    ``.claude/scripts/`` (what user projects run), falls back to the source
    template. Returns None when neither exists (soft-skip)."""
    candidates = [
        repo_root / ".claude" / "scripts" / "analyze_code_graph.py",
        repo_root / "templates" / "scripts" / "analyze_code_graph.py",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def build_resync_deferral(
    project_name: str,
    command_to_apply: str,
) -> Optional[object]:
    """Construct the ``codegraph_embed_resync_pending`` :class:`DeferralEntry`.

    Returns None when ``DeferralEntry`` is unavailable (isolated import) so the
    caller can still branch on a falsy value without a hard dependency.
    """
    if DeferralEntry is None:
        return None
    return DeferralEntry(
        condition_id=_CONDITION_ID,
        title="Code-graph re-embed pending (chunking revision changed)",
        detected=(
            "The code-embedding service (:{port}) was unreachable during "
            "--update, so the revision-gated code-graph resync for project "
            "'{proj}' could not run. About 7-9% of functions/classes were "
            "embedded under the pre-chunking scheme and need re-embedding so "
            "their over-budget tails become searchable.".format(
                port=DEFAULT_CODE_EMBED_PORT, proj=project_name
            )
        ),
        why_deferred=(
            "A per-object re-embed needs the code-embedding service running; "
            "launching the analyze now would fail every embed. The resync is "
            "resumable — re-running it once the service is up re-embeds only "
            "the stale rows (revision mismatch) and skips everything already "
            "current, so it is safe and cheap to defer."
        ),
        command_to_apply=command_to_apply,
        severity="warning",
        kg_node_refs=[],
    )


def spawn_background_resync(
    repo_root: Path,
    project_name: str,
    *,
    python_exe: Optional[str] = None,
    code_embed_url: Optional[str] = None,
    check_service: bool = True,
    index_dot_claude: bool = True,
) -> ResyncTriggerResult:
    """Launch a BACKGROUND, revision-gated full re-analyze of ``repo_root``.

    A full (no ``--incremental``, no ``--only-file``) analyze is intentional:
    the revision gate inside the analyzer makes it LIGHT — only rows whose
    stored ``embed_revision`` differs from the current one re-embed; the 90%+
    already-current rows hash-skip. This is the host-agnostic, revision-based
    resync (it re-embeds the root project too, not just non-root projects).

    Non-blocking: the analyzer is spawned detached via ``Popen`` and this
    function returns immediately with ``status="launched"``. NO global timeout
    is imposed — the analyzer self-guards per embed request.

    Degrade path: when ``check_service`` and the code-embed service is down, we
    do NOT spawn (a re-embed would fail). We return ``status="deferred"`` with a
    :class:`DeferralEntry` for the caller to record; the update still succeeds.

    Never raises: precondition failures (missing analyzer/python, empty project
    name, spawn error) return a ``skipped``/``deferred`` result with a message.
    """
    if not project_name:
        return ResyncTriggerResult(
            status="skipped", message="no project name — cannot target collections"
        )

    analyzer = _resolve_analyzer(repo_root)
    if analyzer is None:
        return ResyncTriggerResult(
            status="skipped",
            message=f"analyze_code_graph.py not found under {repo_root}",
        )

    py = python_exe or sys.executable
    if not py:
        return ResyncTriggerResult(
            status="skipped", message="no python interpreter resolved"
        )

    resume_cmd = (
        f"{py} {analyzer} {repo_root} --project {project_name}"
    )

    if check_service and not code_embed_service_healthy(code_embed_url):
        deferral = build_resync_deferral(project_name, resume_cmd)
        return ResyncTriggerResult(
            status="deferred",
            message=(
                f"code-embed service (:{DEFAULT_CODE_EMBED_PORT}) unreachable — "
                "resync deferred (see UPDATE_DEFERRED.md)"
            ),
            deferral=deferral,
        )

    # Build the analyze argv. Full walk (revision gate keeps it light). We do
    # NOT pass --force-recreate (that would DROP + rebuild the schema, losing
    # all rows) — the resync is purely additive re-embed of stale rows.
    # (F11-i: the dead `canonical_source` parameter was removed — it was never
    # wired into the argv and full-walk resync has no use for it.)
    argv = [py, str(analyzer), str(repo_root), "--project", project_name]

    # Detached background spawn. Redirect stdout/stderr to DEVNULL so the
    # child outlives the parent and doesn't hold the parent's pipes open.
    # start_new_session detaches the process group (POSIX); on Windows the
    # default is fine (no controlling terminal to inherit).
    popen_kwargs = {
        "cwd": str(repo_root),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    # F9: spawn the ignore-set prune as a SECOND detached child (this module
    # run as a script — see the __main__ handler). Background, soft-fail:
    # a prune spawn failure never blocks the resync itself. Rows it deletes
    # are regenerable derived data; the concurrent analyzer never re-writes
    # them (its walkers skip the same ignore set).
    try:
        prune_argv = [
            py, str(Path(__file__).resolve()),
            "--prune-ignored", "--project", project_name,
        ]
        if index_dot_claude:
            prune_argv.append("--index-dot-claude")
        subprocess.Popen(prune_argv, **popen_kwargs)  # noqa: S603 — argv is ours
    except Exception as exc:  # noqa: BLE001 — prune is best-effort
        logger.warning("codegraph prune spawn failed: %s", exc)

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603 — argv is ours
    except Exception as exc:  # noqa: BLE001 — spawn failure must not crash update
        # Treat a spawn failure like the service-down case: defer with a
        # re-run command so the user can complete the resync later.
        deferral = build_resync_deferral(project_name, resume_cmd)
        return ResyncTriggerResult(
            status="deferred",
            message=f"background analyze spawn failed: {exc}",
            deferral=deferral,
        )

    return ResyncTriggerResult(
        status="launched",
        message=f"background code-graph resync launched for {project_name}",
        pid=proc.pid,
    )


def _main(argv: Optional[list] = None) -> int:
    """Script entrypoint — used by the detached prune child spawned from
    :func:`spawn_background_resync`. Only the prune mode exists; the analyze
    itself is a separate child (the analyzer script)."""
    import argparse

    # Script mode puts THIS file's directory (vco_lib/) on sys.path, not the
    # repo root — make `vco_lib.*` importable for the prefix resolver.
    _repo_root = str(Path(__file__).resolve().parent.parent)
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    parser = argparse.ArgumentParser(description="codegraph resync helpers")
    parser.add_argument("--prune-ignored", action="store_true",
                        help="delete rows whose path is in the ignore set")
    parser.add_argument("--project", required=True, help="project name")
    parser.add_argument("--index-dot-claude", action="store_true",
                        help="the project indexes .claude/ — do NOT prune it")
    args = parser.parse_args(argv)

    if args.prune_ignored:
        logging.basicConfig(level=logging.INFO)
        counts = prune_ignored_rows(
            args.project, index_dot_claude=args.index_dot_claude,
        )
        total = sum(counts.values()) if counts else 0
        logger.info("codegraph prune complete: %d row(s) deleted (%s)",
                    total, counts)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
