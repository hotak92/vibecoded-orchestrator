# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Did this analyzer run actually analyze anything? (v0.2.105 fix round)

The analyzer SOFT-SKIPS when no embedding backend answers: it prints
``⚠️  Code-graph analysis skipped: …`` to stderr and exits **0**, having
indexed nothing (``templates/scripts/analyze_code_graph.py`` — both the
"no backend" and the "backend not reachable" arms). Exit status alone
therefore cannot tell a successful walk from a skipped one, and a caller
that reads only ``ok`` records a baseline — or an extra path's
``last_indexed_commit`` — over a graph that was never walked (review
MEDIUM: the HEAD catch-up stamped HEAD after a skipped full walk, so the
gap was never revisited).

This module is the ONE home for "the analyzer said it skipped", so the two
consumers cannot drift apart (the same class was fixed for the resync
driver only, and the catch-up kept recording HEAD):

* :mod:`vco_lib.codegraph_extras_refresh` — :func:`run_analyzer` captures
  the child's stdout/stderr, so it checks the captured text
  (:func:`says_analysis_skipped`). That runner is shared by the HEAD
  catch-up (:mod:`vco_lib.codegraph_head_catchup`), the extras refresh,
  the queued extra drain and the missing-extras heal.
* :mod:`vco_lib.codegraph_resync` — the resync driver spawns the analyzer
  itself with the run's shared log as its stdout/stderr, so it reads that
  log's tail window (:func:`log_says_analysis_skipped`).

The log is per-spawn since v0.2.105 (the file name carries a nanosecond
stamp + pid, so a second spawn can never append to — or read — the first
run's log).

Split out of ``vco_lib/codegraph_resync.py`` (the file-size rule — that
module is past 5,600 lines); the Weaviate/git plumbing stays there.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ANALYZER_SOFT_SKIP_MARKER",
    "SOFT_SKIP_REASON",
    "log_says_analysis_skipped",
    "read_log_window",
    "says_analysis_skipped",
    "soft_skip_reason",
]

#: The analyzer's soft-skip marker (printed to stderr, exit 0). MUST MATCH
#: the literal in ``templates/scripts/analyze_code_graph.py``.
ANALYZER_SOFT_SKIP_MARKER = "Code-graph analysis skipped"

#: The bounded reason an :class:`AnalyzerResult` carries for such a run. A
#: caller's failure line then says WHY, instead of the misleading ``ok``.
SOFT_SKIP_REASON = (
    "analysis skipped: the code-embedding backend was unreachable, so "
    "nothing was indexed (analyzer exit 0)"
)


def says_analysis_skipped(text: Optional[str]) -> bool:
    """Does analyzer OUTPUT (stdout, stderr, or a log window) carry the
    soft-skip marker?"""
    if not text:
        return False
    return ANALYZER_SOFT_SKIP_MARKER in text


def soft_skip_reason() -> str:
    """The failure reason an :class:`AnalyzerResult` carries for a skipped
    run (see :data:`SOFT_SKIP_REASON`)."""
    return SOFT_SKIP_REASON


def read_log_window(log_path: "Path | str | None", max_bytes: int) -> Optional[str]:
    """The last ``max_bytes`` of a shared per-spawn log, or ``None``.

    ``None`` when there is no path (a spawn that degraded to DEVNULL), the
    file is missing, or it cannot be read — callers treat that as "cannot
    tell", never as "the marker matched" or as "no skip". Soft-fail by
    contract: a best-effort read never gates the caller.
    """
    if not log_path:
        return None
    try:
        path = Path(log_path)
        size = path.stat().st_size
        with open(path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — best-effort reads never gate
        logger.debug("codegraph: log-window read skipped: %s", exc)
        return None


def log_says_analysis_skipped(
    log_path: "Path | str | None", max_bytes: int
) -> bool:
    """Did THIS run's analyzer soft-skip, judged from its shared log?

    Only the log of this run is read: the driver's spawn log is a fresh file
    per spawn (unique name), so a previous run's marker line cannot match.
    """
    return says_analysis_skipped(read_log_window(log_path, max_bytes))