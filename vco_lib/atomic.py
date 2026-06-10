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

v0.2.53 lands the module with these three exports; install.py's
``.claude.json`` write site still uses
``vco_lib.env_template._atomic_write_text`` directly (consolidating
the two implementations is a v0.2.54 organisational refactor).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


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
