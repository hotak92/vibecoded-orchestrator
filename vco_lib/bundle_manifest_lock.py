# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One per-project lock around every writer of the bundle manifest (v0.2.106).

Three writers read ``.claude/.vco-manifest.json`` and write it back: the
bundle engine (:func:`vco_lib.project_init.install_project_bundle`, a long
run), the provider switch's immediate apply (:mod:`vco_lib.model_providers`,
a short one) and a deferral dismissal's memory
(:func:`vco_lib.deferral_dismissal.record_dismissal`, review L3). Each write
is atomic, but none was exclusive, so a switch flipped while "Update all" ran
could be undone by the update writing the manifest it read at its start — the
parked-agents registry lost, a definition delivered twice (review C-M2).

All three now hold :func:`bundle_manifest_lock` for the whole
read-modify-write:

* the engine waits for it, and says so: after :data:`ENGINE_NOTICE_FIRST_S`
  a "waiting for another bundle run" line on stderr, then one a minute, and
  after :data:`ENGINE_WAIT_S` it gives up with :class:`BundleRunBusy` naming
  the lockfile (review L8 — a silent wait looked like a hang);
* the apply waits a bounded time (:data:`APPLY_WAIT_S`) and, when an update
  is still running, reports the project instead of moving anything — the
  user runs Update bundle on it once that update finishes (the update
  applies the stored switch);
* a dismissal waits :data:`DISMISSAL_WAIT_S`, then records nothing (its
  memory is best-effort; the dismissal itself already happened).

The lockfile lives under ``.claude/state/`` (git-excluded VCO runtime state)
and is a pure token: its content is irrelevant and it may stay on disk. A
dry run writes nothing and takes no lock. NOT re-entrant (POSIX ``flock`` on
a second descriptor blocks the same process): never call the engine or the
apply from inside the other's locked block.
"""
from __future__ import annotations

import functools
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypeVar

from vco_lib.atomic import LockTimeout

__all__ = [
    "LOCK_REL",
    "APPLY_WAIT_S",
    "DISMISSAL_WAIT_S",
    "ENGINE_WAIT_S",
    "BundleRunBusy",
    "bundle_manifest_lock",
    "serialized_bundle_run",
]

#: The lockfile, relative to a project folder.
LOCK_REL = Path(".claude") / "state" / "bundle-manifest.lock"

#: How long the provider switch's apply waits for a running bundle update.
APPLY_WAIT_S = 20.0

#: How long a dismissal's memory write waits for a running bundle update.
DISMISSAL_WAIT_S = 120.0

#: The engine's wait: the first "waiting" line, the interval between lines,
#: and the upper bound. Long by design — an update of a large project on a
#: slow machine holds the lock for many minutes, and giving up early would
#: fail a run that only had to wait.
ENGINE_NOTICE_FIRST_S = 5.0
ENGINE_NOTICE_EVERY_S = 60.0
ENGINE_WAIT_S = 4 * 3600.0


class BundleRunBusy(LockTimeout):
    """Another bundle run held the project's manifest lock past
    :data:`ENGINE_WAIT_S`; this run did nothing."""

F = TypeVar("F", bound=Callable[..., Any])


@contextmanager
def bundle_manifest_lock(folder: "Path | str", *,
                         timeout_s: Optional[float] = None) -> Iterator[None]:
    """Hold the project's bundle-manifest lock for the block.

    ``timeout_s`` ``None`` waits; a number raises
    :class:`vco_lib.atomic.LockTimeout` before the block runs. A folder with
    no ``.claude`` directory is not locked — it has no manifest to race on,
    and the lock must never be what creates ``.claude`` in a folder — and
    neither is one whose ``.claude`` is a symlink (the lockfile would land
    outside the project; the engine's own symlink guards decide there).
    """
    from vco_lib.atomic import exclusive_file_lock  # noqa: PLC0415

    folder = Path(folder)
    claude = folder / ".claude"
    if claude.is_symlink() or not claude.is_dir():
        yield
        return
    with exclusive_file_lock(folder / LOCK_REL, timeout_s=timeout_s):
        yield


def serialized_bundle_run(fn: F) -> F:
    """Decorate the bundle engine's entry point: a run that may write (not a
    dry run, not the classification-only census) holds the project's lock."""

    @functools.wraps(fn)
    def run(folder: Any, *args: Any, **kwargs: Any) -> Any:
        writes = not (kwargs.get("dry_run") or kwargs.get("_classification_only"))
        target: Path | None = None
        try:
            target = Path(folder).resolve()
            usable = target.is_dir()
        except (OSError, TypeError, ValueError):
            usable = False
        if not writes or not usable or target is None:
            return fn(folder, *args, **kwargs)
        with _engine_lock(target):
            return fn(folder, *args, **kwargs)

    return run  # type: ignore[return-value]


def _engine_lock(target: Path) -> ExitStack:
    """The engine's lock, taken in bounded steps so the wait is visible
    (review L8). Returns an entered :class:`ExitStack` holding the lock — the
    lock is TAKEN here, before the run's body, so a ``LockTimeout`` the body
    itself raises is never mistaken for a busy lock."""
    stack = ExitStack()
    waited, step = 0.0, ENGINE_NOTICE_FIRST_S
    while True:
        try:
            stack.enter_context(bundle_manifest_lock(target, timeout_s=step))
            return stack
        except LockTimeout:
            waited += step
            lock = target / LOCK_REL
            if waited >= ENGINE_WAIT_S:
                raise BundleRunBusy(
                    f"another bundle run on {target} held {lock} for {int(waited)} s; "
                    "this run changed nothing. If no other install or update is running, "
                    "a hung one still holds it — end that process and run the update "
                    "again") from None
            print(f"  ... waiting for another bundle run on {target} to finish "
                  f"(it holds {lock}; waited {int(waited)} s)", file=sys.stderr, flush=True)
            step = min(ENGINE_NOTICE_EVERY_S, ENGINE_WAIT_S - waited)
