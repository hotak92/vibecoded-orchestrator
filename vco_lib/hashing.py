# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""SHA-256 helpers (vco_lib.hashing — v0.2.53).

Consolidates 4 separate sha256 implementations identified in
:file:`.claude/context/audits/vco-lib-python-dedup-2026-06-10.md`
finding 3 (4 sites: project_init.py × 2, diagram_indexer.py,
install.py × 2).

Per docs/INSTALL_ARCHITECTURE_v2.md §7.2.

v0.2.53 lands the module; migration of the 4 callsites is a v0.2.54
organisational refactor (cross-track concern — install.py + vco_lib
both consume).
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk_size: int = 65536) -> str:
    """Return the hex SHA-256 digest of the file at ``path``.

    Args:
        path: File to hash.
        chunk_size: Read-buffer size in bytes (default 64 KB — strikes
            a balance between memory and syscall overhead for the typical
            mid-sized files install.py hashes).

    Raises:
        OSError: file not readable.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str, *, encoding: str = "utf-8") -> str:
    """Return the hex SHA-256 digest of ``text`` after encoding to bytes."""
    return sha256_bytes(text.encode(encoding))
