# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The bundle engine's backup-before-replace writer — the ONE home for
``.claude/backups/bundle-adoptions/<ts>/<dest_rel>``.

Moved out of ``vco_lib.project_init`` in v0.2.100 (WP-15): the leftover pass
(``vco_lib.bundle_leftovers``) is its second caller, and that module is under a
line-count ratchet. ``project_init`` re-exports both names as
``_adopt_backup_timestamp`` / ``_backup_bytes_for_adoption`` — the patch point
the adoption tests use, and the name every caller in the engine resolves — so
a test that makes the backup fail makes it fail for every caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

__all__ = [
    "AdoptionBackupFailed",
    "adopt_backup_timestamp",
    "adopt_write",
    "backup_bytes_for_adoption",
    "bundle_file_mode",
]


class AdoptionBackupFailed(Exception):
    """:func:`adopt_write` could not capture the current bytes, so it wrote
    nothing — the caller keeps the file (the engine's preserve + deferral)."""


def adopt_backup_timestamp() -> str:
    """UTC basic-ISO timestamp for the per-run adoption-backup sub-dir.

    Basic ISO (``20260717T031500Z``) rather than extended (with ``:``) so the
    directory name is filesystem-safe on Windows (``:`` is illegal in NTFS
    path components). One value is computed per install run and reused for
    every file adopted in that run (a single ``<ts>`` dir per run per D7).
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_bytes_for_adoption(
    folder: Path, dest_rel: str, ts: str, current_bytes: bytes,
) -> str:
    """v0.2.84 PLAN-v0284 D7 (P5/R2): copy the CURRENT on-disk bytes of a file
    about to be ADOPTED into the per-run backup tree, atomically.

    Backup layout::

        <folder>/.claude/backups/bundle-adoptions/<ts>/<dest_rel>

    ``dest_rel`` is the bundle destination-relative path (e.g.
    ``.claude/hooks/foo.sh``), reused verbatim under the timestamp dir so the
    backup mirrors the project tree and is trivially discoverable. Uses the
    shared ``_write_file_atomic`` primitive (parents created, atomic replace,
    symlink guards apply).

    Returns the backup path RELATIVE to ``folder`` (POSIX-normalised, for the
    NOTICE / JSONL trail). Raises on any write failure — the caller MUST treat a
    raise as "do NOT adopt" and fall back to preserve + deferral (never destroy
    bytes without a captured copy).

    v0.2.84 PLAN-v0284 AMENDMENTS A4: ``dest_rel`` is host-OS-shaped (``_enumerate_bundle_
    files`` builds it via ``str(Path(...))`` → ``knowledge\\concepts\\foo.md`` on
    Windows). We normalize the separator to ``/`` via the shared
    ``vco_lib.paths.to_posix_rel`` helper (the v0.2.81 lesson — never inline a
    2nd copy) and JOIN via the individual POSIX parts so the backup mirror tree
    is byte-identical across OSes AND stays path-length-aware (component-wise
    join, no monolithic string that could overflow a Windows MAX_PATH check).
    """
    from vco_lib.paths import to_posix_rel
    from vco_lib.project_backups import ensure_backups_dir
    from vco_lib.project_init import _ADOPT_BACKUPS_REL, _write_file_atomic

    # The backup tree is self-ignoring (review R11 A1): a raise here is a
    # backup failure like any other — the caller does not adopt.
    ensure_backups_dir(folder)
    rel_parts = PurePosixPath(to_posix_rel(dest_rel)).parts
    backup_abs = folder / _ADOPT_BACKUPS_REL / ts
    for part in rel_parts:
        backup_abs = backup_abs / part
    # `_write_file_atomic` may redirect through a symlink-blocking `.vco-new`
    # sibling; if it does, the ORIGINAL backup destination did not receive the
    # bytes. Treat a redirect as a backup failure (be conservative — we must
    # have the bytes at the documented path before we overwrite the original).
    redirect = _write_file_atomic(backup_abs, current_bytes)
    if redirect is not None:
        raise OSError(
            f"adoption backup for {dest_rel} was redirected to a .vco-new "
            f"sibling ({redirect}) — refusing to adopt without a captured copy "
            "at the documented backup path"
        )
    backup_rel = PurePosixPath(to_posix_rel(str(_ADOPT_BACKUPS_REL))) / ts
    for part in rel_parts:
        backup_rel = backup_rel / part
    return str(backup_rel)


def bundle_file_mode(dest_rel: str) -> Optional[int]:
    """The file mode the engine writes a bundle file with: ``0o700`` for a
    shell script or anything under ``.claude/scripts/`` (many have no
    extension), else ``None`` (the umask default). 0o700, not 0o755/0o750:
    CodeQL py/overly-permissive-file flags group/world access, and the
    project folder belongs to the user."""
    from vco_lib.paths import to_posix_rel

    if dest_rel.endswith(".sh") or "/scripts/" in to_posix_rel(dest_rel):
        return 0o700
    return None


def adopt_write(
    folder: Path, dest_rel: str, target_path: Path, source_bytes: bytes, ts: str,
) -> tuple[str, Optional[Path]]:
    """The engine's ``adopt`` action for ONE file (v0.2.84 D7): back up the
    CURRENT bytes under ``bundle-adoptions/<ts>/``, THEN write the shipped
    bytes. Returns ``(backup_rel, redirect)`` — ``redirect`` is the
    ``.vco-new`` sibling a symlink-blocked write went to, else ``None``.

    Raises :class:`AdoptionBackupFailed` when the backup could not be taken
    (nothing was written), and lets a failed shipped write raise as itself
    (the backup exists, the file is unchanged — the atomic write never
    replaces partially). The backup goes through
    ``vco_lib.project_init._backup_bytes_for_adoption`` — the patch point the
    adoption tests use — so a test that fails the backup fails it for every
    caller: the bundle engine's loop and the provider switch's restore
    (v0.2.106 review M2) alike.
    """
    from vco_lib import project_init as _pi

    try:
        current = target_path.read_bytes()
        backup_rel = _pi._backup_bytes_for_adoption(folder, dest_rel, ts, current)
    except Exception as exc:  # noqa: BLE001 — any backup failure means "do not adopt"
        raise AdoptionBackupFailed(f"{type(exc).__name__}: {exc}") from exc
    redirect = _pi._write_file_atomic(target_path, source_bytes,
                                      mode=bundle_file_mode(dest_rel))
    return backup_rel, redirect
