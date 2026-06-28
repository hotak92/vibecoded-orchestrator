# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Bounded HTTP helper shared by every embedding adapter.

v0.2.70 FIX A — total per-request deadline for embed POSTs
==========================================================

Background — the wedge this fixes
---------------------------------
Each embed adapter (:mod:`ollama`, :mod:`codeembed`, :mod:`openai`)
used to call ``session.post(url, ..., timeout=self.timeout)`` directly.
``requests`` interprets a scalar ``timeout`` as ``(connect, read)`` where
the *read* component is the **maximum gap between received bytes**, NOT a
total wall-clock deadline for the whole request. A backend that keeps the
socket open and dribbles a byte (or a TCP keep-alive) more often than
``timeout`` seconds therefore **resets the read clock on every dribble and
never trips the timeout** — the request hangs forever.

That is exactly what was observed on the ``install.py --update`` seed path:
an arctic re-embed of 2587 shared-KG nodes on CPU sat in I/O-wait on Ollama
for ~34 min (sockets open, no forward progress) until a manual kill, because
the scalar 180s read-gap timeout could not catch a no-forward-progress wedge.

The fix (per the maintainer's standing ruling)
-----------------------------------------------
The maintainer forbids any process/node/file-level timeout (a process kill
on a slow-but-healthy seed strands the user mid-install — worse than slow).
The ONLY permitted guard granularity is the single embed HTTP request — i.e.
ONE chunk (KG single-text embed) or ONE batch (docs/code batch embed). Both
are a single ``session.post`` call. A node may contain ~1000 chunks; each
chunk is a SEPARATE POST, so bounding the POST bounds the chunk/batch and is
never per-node, per-file, or per-process.

:func:`bounded_post` runs the ``session.post`` under a hard *total* wall-clock
deadline using a single worker thread + :meth:`concurrent.futures.Future.result`.
If the full request (connect + send + receive headers + receive body) has not
completed within ``timeout`` seconds, the call abandons the in-flight socket
and raises ``requests.Timeout`` — which every adapter already wraps as
``RuntimeError`` and surfaces to the caller. The sync loop then logs that one
chunk's failure and moves on, instead of hanging the whole process.

Why this does NOT kill a slow-but-progressing chunk
---------------------------------------------------
The deadline is the SAME generous value the scalar timeout used (180s default,
~6x the observed ~30s/chunk arctic-on-CPU boundary, raisable via
``VCT_EMBED_REQUEST_TIMEOUT_SECS``). A chunk that *completes* under the
deadline returns its real response before the future deadline elapses — it is
never aborted. Only a request that makes no forward progress past the deadline
(the dribble/hung-socket wedge) is failed. The original scalar
``(connect, read)`` timeout is still passed to ``requests`` underneath as a
faster floor for the clean connection-refused / fully-silent-socket cases.

Thread-abandonment caveat
--------------------------
When the deadline fires we cannot forcibly kill the worker thread (Python has
no safe thread-cancel). The worker is a *daemon* thread, so it does not block
interpreter exit, and the abandoned socket is reclaimed by the OS /
``requests`` connection pool. In the wedge case the thread is parked in
I/O-wait and will unwind when the socket is eventually closed or the process
exits — it holds no locks the caller needs. This is the standard, accepted
trade-off for bounding an un-cancellable blocking C-level socket read.
"""

from __future__ import annotations

import concurrent.futures
import threading

import requests

# A single shared daemon thread pool for ALL bounded embed POSTs across every
# adapter in the process. One worker is plenty: embedding is sequential within
# a sync loop (one chunk at a time), and a bounded queue means a wedged worker
# simply gets abandoned while the next POST spins up a fresh worker. Daemon
# threads never block interpreter shutdown.
_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: "concurrent.futures.ThreadPoolExecutor | None" = None


def _get_executor() -> "concurrent.futures.ThreadPoolExecutor":
    """Lazily create the shared daemon thread pool (thread-safe)."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="embed-bounded-post",
            )
        return _EXECUTOR


def bounded_post(
    session: requests.Session,
    url: str,
    *,
    timeout: float,
    **post_kwargs: object,
) -> requests.Response:
    """POST ``url`` on ``session`` under a TOTAL per-request wall-clock deadline.

    This is a drop-in replacement for ``session.post(url, timeout=timeout,
    **kwargs)`` that additionally bounds the *whole* request (not just the
    inter-byte read gap) by ``timeout`` seconds. See the module docstring for
    why the scalar ``requests`` timeout alone cannot catch a dribbling-socket
    wedge.

    The underlying ``session.post`` still receives ``timeout`` as its scalar
    ``(connect, read)`` value — that remains the fast floor for clean failures
    (connection refused, fully-silent socket). The added Future-deadline is the
    backstop for the no-forward-progress case.

    Args:
        session: The (pooled) ``requests.Session`` to issue the POST on.
        url: Target URL.
        timeout: Total wall-clock deadline in seconds for the entire request.
            Must be positive (callers resolve it from
            ``VCT_EMBED_REQUEST_TIMEOUT_SECS`` / the 180s default, both
            guaranteed positive).
        **post_kwargs: Forwarded verbatim to ``session.post`` (``json=``,
            ``headers=``, ``data=``, ...). A caller-supplied ``timeout`` in
            ``post_kwargs`` is ignored in favour of the explicit ``timeout``
            argument (one deadline, no ambiguity).

    Returns:
        The :class:`requests.Response` from the completed POST.

    Raises:
        requests.Timeout: If the full request does not complete within
            ``timeout`` seconds (the wedge case). Also raised when the
            underlying ``requests`` scalar timeout fires first.
        requests.RequestException: Any other transport error raised by
            ``session.post`` (connection refused, DNS failure, ...), propagated
            unchanged so adapters' existing ``except RequestException`` handlers
            keep working.
    """
    # The scalar timeout floor still applies inside requests; drop any stray
    # timeout kwarg so we control it from one place.
    post_kwargs.pop("timeout", None)

    def _do_post() -> requests.Response:
        return session.post(url, timeout=timeout, **post_kwargs)  # type: ignore[arg-type]

    future = _get_executor().submit(_do_post)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        # The request made no forward progress within the total deadline.
        # Abandon the in-flight socket (the daemon worker unwinds when the OS
        # reclaims it) and surface a Timeout the adapters already handle. We do
        # NOT call future.cancel() — the worker is mid-blocking-IO and cannot be
        # cancelled; leaving it as a daemon is the accepted trade-off.
        raise requests.Timeout(
            f"embed request to {url} exceeded total deadline of {timeout}s "
            "(no forward progress — wedged/dribbling backend); failing this "
            "request so the sync can continue"
        ) from exc
