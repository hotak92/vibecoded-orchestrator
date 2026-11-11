# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The wall-clock bound of the doctor probe running on THIS thread (v0.2.106).

``vco doctor`` runs each probe under its own bound
(:func:`vco_lib.doctor._run_probe_bounded`). A probe that overruns is reported
"not checked" and its worker thread is abandoned — Python cannot stop a
thread — so without this module that thread could keep going and WRITE
later: emit or resolve a ledger entry after the report said nothing was
checked, possibly after install.py's single authoritative ledger write
(review C-M3).

The bound is thread-local state set only on a doctor probe's worker thread:

* :func:`deadline` — the monotonic time the probe's bound ends, so a probe
  with a long legitimate runtime (the bundle census) can stop starting new
  work in time and report what it did not reach;
* :func:`cancelled` — True once the doctor gave up on the probe. Every
  writer a probe can reach checks it and writes NOTHING when it is set: the
  locked ledger emitter (:func:`vco_lib.deferral_emit.locked_report`), the
  census state + ledger refresh (:mod:`vco_lib.bundle_staleness`) and the
  Weaviate case-history ledger (:mod:`vco_lib.weaviate_case_history`).

Outside a doctor probe (any other thread, any other caller) :func:`deadline`
is ``None`` and :func:`cancelled` is ``False`` — nothing changes.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional

__all__ = ["bounded", "deadline", "cancelled", "publish"]

_local = threading.local()


@contextmanager
def bounded(until: float, cancel: threading.Event,
            partial: Optional[List[Any]] = None) -> Iterator[None]:
    """Mark the CURRENT thread as a doctor probe bound to ``until``
    (``time.monotonic()``) and cancelled once ``cancel`` is set.
    ``partial`` receives what the probe hands over with :func:`publish`."""
    prior = (getattr(_local, "until", None), getattr(_local, "cancel", None),
             getattr(_local, "partial", None))
    _local.until, _local.cancel, _local.partial = until, cancel, partial
    try:
        yield
    finally:
        _local.until, _local.cancel, _local.partial = prior


def publish(item: Any) -> None:
    """Hand one finding to the doctor AS the probe goes (review L2): when the
    bound cancels the probe, the doctor reports these beside its "not
    checked" line instead of discarding everything the probe had found. A
    no-op outside a doctor probe, and once the probe is cancelled."""
    sink = getattr(_local, "partial", None)
    if sink is not None and not cancelled():
        sink.append(item)


def deadline() -> Optional[float]:
    """The monotonic deadline of the probe on this thread, else ``None``."""
    return getattr(_local, "until", None)


def cancelled() -> bool:
    """True when this thread runs a doctor probe the doctor has given up on:
    it must not write anything any more."""
    cancel = getattr(_local, "cancel", None)
    return bool(cancel is not None and cancel.is_set())
