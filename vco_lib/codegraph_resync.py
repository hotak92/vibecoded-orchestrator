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

import os
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


DEFAULT_CODE_EMBED_PORT = 11440
_CONDITION_ID = "codegraph_embed_resync_pending"


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
    canonical_source: Optional[str] = None,
    check_service: bool = True,
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
    argv = [py, str(analyzer), str(repo_root), "--project", project_name]
    if canonical_source:
        # Only meaningful in single-file mode for the analyzer; full-walk resync
        # doesn't use it. Kept for forward-compat / explicit callers.
        pass

    try:
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
