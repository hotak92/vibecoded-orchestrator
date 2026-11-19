# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""ONE cross-process "check, then spawn" lock for the three service ensures.

:mod:`vco_lib.hub_ensure`, :mod:`vco_lib.gateway_ensure` and
:mod:`vco_lib.launcher_ensure` each answer "is it running? if not, start it".
Without a lock that is a check-then-act race: two callers that ask in the same
instant both see "not running" and both spawn. It is not hypothetical — the
same SessionStart hook runs twice on every project open (Claude Code's
``SessionStart`` AND ``.vscode/tasks.json``'s ``folderOpen`` task), several
windows restore at once at login, and a 2026-11 Windows field report had two
launchers start together at boot.

The mechanism, per service
--------------------------
:func:`spawn_lock` holds :func:`vco_lib.atomic.exclusive_file_lock` (a real
``flock`` on POSIX, an ``msvcrt`` byte lock on Windows) on
``<vct_root>/state/ensure-<service>.lock`` around the ensure's check AND its
spawn, and the holder keeps it until the started process ANSWERS
(:func:`wait_until_up`) or a bound expires. A second caller blocks on the lock
for a little longer than that bound; when it gets the lock it runs its own
check, which now finds the service running, and spawns nothing.

Why the lock cannot wedge
-------------------------
* A kernel lock is released when its holder exits, crash and ``kill -9``
  included, so a dead holder never blocks anyone: there is no stale-lock
  state to detect and no TTL to tune.
* A LIVE holder holds it for at most its settle bound plus the spawn call.
  A waiter that still times out (a holder wedged inside its spawn) proceeds
  WITHOUT the lock (:func:`spawn_lock` yields ``False``) rather than leaving a
  service down: every one of the three services has its own single-instance
  guard as the backstop — ``hub.pid`` is claimed under ``O_CREAT|O_EXCL`` by
  the hub, the gateway daemon claims its pid file the same way, and the
  launcher takes a per-user instance lock as the first thing in ``run()``.
  The lock makes the ensure honest and spawn-once; those guards make a
  duplicate harmless.
* The lock file is a pure token (contents irrelevant) and is never inherited
  by the spawned child: Python opens it non-inheritable (PEP 446), and
  ``Popen`` closes other descriptors by default.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

from vco_lib.paths import vct_root_dir

__all__ = [
    "LOCK_MARGIN_S",
    "SERVICES",
    "ensure_lock_path",
    "spawn_lock",
    "wait_until_up",
]

#: The services that take this lock. The names are part of the lock file
#: names, which the uninstaller removes by exact name
#: (:mod:`vco_lib.uninstall_artifacts`).
SERVICES: tuple[str, ...] = ("hub", "gateway", "launcher")

#: How much longer than the holder's settle bound a waiter waits for the lock.
#: Covers the spawn call itself (a ``Popen``, a ``systemctl --user start``).
LOCK_MARGIN_S = 10.0

#: Poll interval of :func:`wait_until_up`.
_POLL_S = 0.1


def ensure_lock_path(service: str, root: Optional[Path] = None) -> Path:
    """``<vct_root>/state/ensure-<service>.lock`` — per user, per service."""
    if service not in SERVICES:
        raise ValueError(f"unknown ensure service {service!r} (known: {', '.join(SERVICES)})")
    return (root if root is not None else vct_root_dir()) / "state" / f"ensure-{service}.lock"


@contextlib.contextmanager
def spawn_lock(
    service: str, *, settle_s: float, root: Optional[Path] = None,
) -> Iterator[bool]:
    """Hold the ``service`` ensure lock for the block.

    Waits up to ``settle_s + LOCK_MARGIN_S`` for another holder. Yields
    ``True`` when the lock is held; ``False`` when the wait expired or the lock
    file cannot be created (an unwritable state dir) — the block then runs
    unlocked, see the module docstring for why that is the safe direction.
    """
    from vco_lib.atomic import LockTimeout, exclusive_file_lock

    path = ensure_lock_path(service, root)
    with contextlib.ExitStack() as stack:
        # Only the ACQUISITION is guarded here; an exception raised by the
        # caller's block propagates untouched.
        try:
            stack.enter_context(
                exclusive_file_lock(path, timeout_s=max(0.0, settle_s) + LOCK_MARGIN_S)
            )
            held = True
        except (LockTimeout, OSError):
            held = False
        yield held


def wait_until_up(
    is_up: Callable[[], bool],
    *,
    bound_s: float,
    poll_s: float = _POLL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll ``is_up`` until it answers ``True`` or ``bound_s`` elapses.

    Asked at least once, even with ``bound_s <= 0``. A probe that raises
    counts as "not up yet". Returns the last answer. Never kills, never
    retries a spawn: it only decides how long the caller keeps the lock.
    """
    deadline = clock() + max(0.0, bound_s)
    while True:
        try:
            if is_up():
                return True
        except Exception:  # noqa: BLE001 — a failing probe is "not up yet"
            pass
        if clock() >= deadline:
            return False
        sleep(poll_s)
