# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Atomic file writes with crash-safety (vco_lib.atomic — v0.2.53).

Centralises the ``<x>.tmp + os.replace`` pattern used 8+ times in
install.py and matches the existing
``vco_lib.env_template._atomic_write_text`` (which v0.2.53 DEDUP-5
migrated install.py's ``.claude.json`` write site onto).

Per docs/INSTALL_ARCHITECTURE_v2.md §7.1.

Design constraints:

* **Cross-OS atomic rename**: ``os.replace`` is documented atomic on
  POSIX (rename(2)) and Windows (MoveFileExW with
  ``MOVEFILE_REPLACE_EXISTING``). The tempfile lives in the SAME
  directory as the target so the rename stays on one filesystem
  (cross-FS rename raises EXDEV on Linux).

* **Crash-safety**: ``tmp.flush()`` + ``os.fsync()`` BEFORE
  ``os.replace``. Closes the power-loss-leaves-empty-file bug noted
  in :file:`.claude/context/audits/install-py-dedup-2026-06-10.md`
  finding #13. Fsync errors on pseudo-filesystems (procfs, tmpfs in
  containers) are tolerated (caller may opt-out via ``fsync=False``).

* **Cleanup on error**: tempfile is unlinked on any exception before
  re-raising. No ``.tmp`` leaks on any code path. This is the
  property that closed CORRECT-1 in v0.2.53.

Public surface:

* :func:`atomic_write_text` — write str to file, atomically.
* :func:`atomic_write_bytes` — write bytes to file, atomically.
* :func:`atomic_write_json` — write JSON object, atomically.
* :func:`exclusive_file_lock` — cross-platform exclusive file lock
  (``contextmanager``); best-effort no-lock on platforms without
  ``fcntl`` (Windows).

v0.2.53 landed the module with these three exports. v0.2.54 Track J
completed the consolidation this paragraph used to queue: the sibling
copies in ``env_template`` / ``config_projection`` /
``deferral_report`` / ``cli/codegraph_diagram`` (plus the inline block
in ``DeferralReport.write``) are now thin delegates to
:func:`atomic_write_text`. install.py's ``.claude.json`` write site
keeps importing ``env_template._atomic_write_text`` (same
implementation, one hop).

v0.2.83 (WP-B1) moved the ``fcntl.flock`` idiom that
``vco_lib.resolver_warn`` had hand-written (``_acquire_lock`` /
``_release_lock``) into :func:`exclusive_file_lock` here — one-home
modularity — so the deferral emitter (``vco_lib.deferral_emit``) and
``resolver_warn`` share ONE cross-platform lock implementation.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator


def atomic_write_text(
    path: Path,
    body: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
) -> None:
    """Write ``body`` to ``path`` atomically with the given encoding.

    Args:
        path: Destination file path.
        body: Text content (LF line endings will be preserved as-is
            from ``body``; this helper does NOT translate line endings).
        encoding: Character encoding (default ``"utf-8"``).
        fsync: Whether to fsync before rename (default True). Set
            False for pseudo-filesystems where fsync may raise
            spuriously.

    Crash-safety: tempfile is fsync'd to disk before ``os.replace``.
    On any exception the tempfile is unlinked and the exception
    re-raised. No ``.tmp`` leftovers on any code path.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(body)
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # fsync can fail on pseudo-filesystems; don't
                    # fail the write over it.
                    pass
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    fsync: bool = True,
) -> None:
    """Write ``data`` to ``path`` atomically (binary mode)."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        os.replace(str(tmp_path), str(path))
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path,
    obj: Any,
    *,
    indent: int = 2,
    sort_keys: bool = False,
    fsync: bool = True,
) -> None:
    """Serialize ``obj`` as JSON and write to ``path`` atomically."""
    body = json.dumps(obj, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
    # Append a trailing newline for POSIX-friendly diffs.
    if not body.endswith("\n"):
        body = body + "\n"
    atomic_write_text(path, body, fsync=fsync)


@contextlib.contextmanager
def exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``lock_path`` for the block.

    Cross-platform, best-effort: on POSIX the lock is a real
    ``fcntl.flock(LOCK_EX)`` on a sidecar lockfile, so concurrent
    processes serialize (the second blocks until the first exits the
    ``with`` block). On Windows — or any platform / filesystem where
    ``fcntl`` is unavailable — the lockfile is still opened but locking
    is skipped (``ImportError`` / ``AttributeError`` / ``OSError``
    fall-through); the block still runs, degrading to no mutual
    exclusion. This mirrors the historical
    ``resolver_warn._acquire_lock`` / ``_release_lock`` posture (v0.2.83
    WP-B1 moved that idiom here so it lives once).

    The lockfile's parent directory is created if missing. The lockfile
    is opened in ``"a+"`` mode (created on first use, never truncated).
    On exit the flock is released (POSIX) and the handle is closed;
    closing implicitly releases the lock too, so a crash inside the
    block can never leave the lock held past process exit.

    Args:
        lock_path: Path to the sidecar lockfile. It is a pure lock token
            — its contents are irrelevant and it is safe to leave on
            disk between runs (typically under a git-ignored directory).

    Yields:
        ``None`` — the caller does the locked work inside the ``with``.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")  # noqa: SIM115 — handle closed in finally
    locked = False
    try:
        try:
            import fcntl  # noqa: PLC0415 — POSIX-only, deferred import

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, AttributeError, OSError):
            # Windows or a filesystem without flock — best-effort
            # fall-through: the block still runs without mutual exclusion.
            locked = False
        yield
    finally:
        if locked:
            try:
                import fcntl  # noqa: PLC0415

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except (ImportError, AttributeError, OSError, ValueError):
                pass
        try:
            fh.close()
        except Exception:  # noqa: BLE001 — closing must never raise into caller
            pass
