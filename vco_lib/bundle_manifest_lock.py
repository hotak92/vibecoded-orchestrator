# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One per-project lock around every writer of the bundle manifest (v0.2.106).

Two writers read ``.claude/.vco-manifest.json``, change the project's agent
files and write the manifest back: the bundle engine
(:func:`vco_lib.project_init.install_project_bundle`, a long run) and the
provider switch's immediate apply (:mod:`vco_lib.model_providers`, a short
one). Each write is atomic, but neither was exclusive, so a switch flipped
while "Update all" ran could be undone by the update writing the manifest it
read at its start — the parked-agents registry lost, a definition delivered
twice (review C-M2).

Both now hold :func:`bundle_manifest_lock` for the whole read-modify-write:

* the engine waits for it (an apply holds it for a moment);
* the apply waits a bounded time (:data:`APPLY_WAIT_S`) and, when an update
  is still running, reports the project instead of moving anything — the
  user runs Update bundle on it once that update finishes (the update
  applies the stored switch).

The lockfile lives under ``.claude/state/`` (git-excluded VCO runtime state)
and is a pure token: its content is irrelevant and it may stay on disk. A
dry run writes nothing and takes no lock. NOT re-entrant (POSIX ``flock`` on
a second descriptor blocks the same process): never call the engine or the
apply from inside the other's locked block.
"""
from __future__ import annotations

import functools
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypeVar

__all__ = ["LOCK_REL", "APPLY_WAIT_S", "bundle_manifest_lock", "serialized_bundle_run"]

#: The lockfile, relative to a project folder.
LOCK_REL = Path(".claude") / "state" / "bundle-manifest.lock"

#: How long the provider switch's apply waits for a running bundle update.
APPLY_WAIT_S = 20.0

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
        try:
            target = Path(folder).resolve()
            usable = target.is_dir()
        except (OSError, TypeError, ValueError):
            usable = False
        if not writes or not usable:
            return fn(folder, *args, **kwargs)
        with bundle_manifest_lock(target):
            return fn(folder, *args, **kwargs)

    return run  # type: ignore[return-value]
