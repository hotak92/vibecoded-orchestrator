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
* :func:`atomic_write_bytes` — write bytes to file, atomically; the ONE
  byte-level writer (``mode=`` and ``symlink_safe=`` options, v0.2.92).
* :func:`rotate_tail_lines` — truncate an append-only log to its tail
  atomically (v0.2.92; was inline in two resolver modules).
* :func:`atomic_write_json` — write JSON object, atomically.
* :func:`atomic_copy_file` — copy a file to a destination atomically,
  metadata-preserving (``copy2`` semantic), with an optional V47-B
  symlink-safe redirect and a best-effort ``soft_fail`` mode. Added in
  the pre-beta cycle (WP-E) as the shared replacement for install.py's
  raw ``shutil.copy2`` overwrite sites.
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
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional


def atomic_write_text(
    path: Path,
    body: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
    mode: Optional[int] = None,
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
        mode: Optional unix permission bits applied to the FINAL path
            after the rename (``os.chmod``). ``mkstemp`` creates the
            tempfile 0600, so the file is never briefly MORE permissive
            than ``mode`` — only less. A chmod ``OSError`` is swallowed
            (chmod is a documented no-op on Windows).

    Crash-safety: tempfile is fsync'd to disk before ``os.replace``.
    On any exception the tempfile is unlinked and the exception
    re-raised. No ``.tmp`` leftovers on any code path.
    """
    atomic_write_bytes(
        path, body.encode(encoding), fsync=fsync, mode=mode,
    )


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    fsync: bool = True,
    mode: Optional[int] = None,
    symlink_safe: bool = False,
) -> Optional[Path]:
    """Write ``data`` to ``path`` atomically (binary mode).

    v0.2.92 (duplication-merge, PLAN-EXTENSION §3.13): this is now the ONE
    byte-level atomic writer — :func:`atomic_write_text` encodes and
    delegates here, and ``vco_lib.project_init._write_file_atomic`` (the
    per-project bundle writer, formerly a 100-line sibling implementation)
    is a thin wrapper over this function with ``symlink_safe=True``.

    Args:
        path: Destination file path.
        data: Bytes to write.
        fsync: Whether to fsync the tempfile before rename (default
            True). Bulk writers (hundreds of small bundle files) may pass
            False to keep the historical no-fsync behaviour.
        mode: Optional unix permission bits applied to the final path
            after the rename; see :func:`atomic_write_text`.
        symlink_safe: When True, if ``path`` (or any ancestor directory)
            is a symlink the bytes are NOT written through it: they land
            at the NEW-8 / V47-B ``.vco-new`` redirect target computed by
            :func:`_symlink_safe_redirect_target` (same convention as
            :func:`atomic_copy_file`), and that target is returned. The
            symlink and its destination are untouched.

    Returns:
        ``None`` on a normal write; the redirect ``Path`` when
        ``symlink_safe`` redirected around a symlink. Existing callers
        that ignore the return value are unaffected.
    """
    target = Path(path)
    redirect: Optional[Path] = None
    if symlink_safe:
        redirect = _symlink_safe_redirect_target(target)
        if redirect is not None:
            target = redirect

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=target.name + ".",
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
                    # fsync can fail on pseudo-filesystems; don't
                    # fail the write over it.
                    pass
        os.replace(str(tmp_path), str(target))
        if mode is not None:
            try:
                os.chmod(str(target), mode)
            except OSError:
                # chmod is a no-op on Windows; don't fail.
                pass
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return redirect


def rotate_tail_lines(path: Path, *, max_bytes: int, keep_lines: int) -> bool:
    """Truncate an append-only text log to its last ``keep_lines`` lines
    once it exceeds ``max_bytes`` — atomically, via :func:`atomic_write_text`.

    v0.2.92 (duplication-merge): ``vco_lib.access_resolver`` and
    ``vco_lib.resolver_warn`` each carried this rotation inline (read all
    lines, keep the tail, write a ``.rot.tmp`` sibling, ``os.replace``).
    Two copies of a routine that decides what data to DISCARD is the wrong
    thing to let drift, so it lives here once.

    Soft-fail by contract: rotation must never break the emit path it
    serves. Any ``OSError`` (unreadable, unwritable, vanished mid-way)
    returns ``False`` and leaves the file as it was — the next call
    retries. Returns ``True`` only when the file was actually rewritten.
    """
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size <= max_bytes:
            return False
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        tail = lines[-keep_lines:] if len(lines) > keep_lines else lines
        atomic_write_text(path, "".join(tail), fsync=False)
        return True
    except OSError:
        return False


def atomic_copy_file(
    src: Path,
    dst: Path,
    *,
    preserve_metadata: bool = True,
    symlink_safe: bool = False,
    fsync: bool = True,
    soft_fail: bool = False,
    on_error: Optional[Any] = None,
) -> Optional[Path]:
    """Copy the file ``src`` to ``dst`` atomically, metadata-preserving.

    The drop-in replacement for ``shutil.copy2(src, dst)`` at call sites
    that overwrite (or may overwrite) an existing destination: unlike
    ``copy2``, which ``open(dst, "wb")``-truncates the FINAL path and
    streams into it, this writes the bytes to a tempfile in ``dst``'s
    parent directory, fsyncs, then ``os.replace``'s it into place. A
    kill / power-loss / disk-full mid-copy therefore leaves either the
    old file intact or the fully-written new one — never a truncated
    node the next step would consume.

    Args:
        src: Source file to copy (read whole into memory once, like the
            small config/binary files at the migrated install.py sites).
        dst: Destination path. Overwritten atomically if it exists.
        preserve_metadata: When True (default) copy ``src``'s stat
            metadata (mtime, mode, …) onto the result via
            :func:`shutil.copystat` — the ``copy2`` semantic. A
            ``copystat`` failure is NOT swallowed: it PROPAGATES (raising
            by default, or routed through ``soft_fail`` / ``on_error``
            when set) so partial-success — a dest left at ``mkstemp``'s
            0600 mode with ``src``'s mtime un-applied — is never silent.
            This matches ``shutil.copy2``, which also propagates copystat
            errors (F-2).
        symlink_safe: When True, if ``dst`` (or any ancestor directory
            under it) is a symlink, VCO does NOT write through it. The
            redirect happens at the SAME LEVEL the symlink lives, matching
            the NEW-8 convention at ``project_init._write_file_atomic``:
            if ``dst`` itself is the symlink the bytes land at ``dst``'s
            ``.vco-new`` sibling; if an ANCESTOR is the symlink the bytes
            land under that ancestor's ``.vco-new`` sibling with the path
            tail replicated (``.claude`` symlinked + ``dst
            == .claude/agents/x`` → ``.claude.vco-new/agents/x``). Either
            way nothing is written through the link into its target, and
            the redirect Path is returned. When False (default) the caller
            is responsible for any symlink decision BEFORE calling (the
            install.py adopt-project sites already gate on
            ``is_symlink_blocking`` / preserve upstream, so they pass False
            to stay behaviour-identical).
        fsync: Whether to fsync the tempfile before rename (default
            True). See :func:`atomic_write_bytes`.
        soft_fail: When True, an ``OSError`` / ``shutil.Error`` during
            the copy is swallowed (``on_error`` is invoked if given) and
            ``None`` is returned instead of raising — for best-effort
            call sites that already sat inside a ``try/except OSError``.
            When False (default) the exception propagates, for load-
            bearing copies.
        on_error: Optional callable invoked with the caught exception
            when ``soft_fail`` swallows it (e.g. a logger). Ignored when
            ``soft_fail`` is False.

    Returns:
        The Path the bytes actually landed at: normally ``dst``; the
        ``.vco-new`` redirect target when ``symlink_safe`` redirected
        around a symlink (leaf sibling for a symlinked ``dst``, or the
        ancestor's ``.vco-new`` sibling + replicated tail for a symlinked
        ancestor); or ``None`` when ``soft_fail`` swallowed an error.

    Note:
        This helper reads ``src`` fully into memory. That matches every
        migrated call site (config JSON, per-file bundle nodes, launcher
        / hub binaries in the tens-of-MB range). It is NOT intended for
        arbitrarily-large streams; those should keep a streaming copy.
    """
    src = Path(src)
    dst = Path(dst)
    target = dst
    try:
        if symlink_safe:
            # V47-B: never write through a symlink at the destination or
            # any ancestor under it. Redirect at the SAME LEVEL the
            # symlink lives — the NEW-8 convention, held once in
            # `_symlink_safe_redirect_target` — so the tempfile+os.replace
            # never touch the symlink's target directory:
            #   * dst itself is a symlink  → `.vco-new` sibling of dst.
            #   * an ANCESTOR is a symlink → `.vco-new` sibling of THAT
            #     ancestor, with the path tail below it replicated
            #     (e.g. `.claude` symlinked, dst `.claude/agents/coder.md`
            #     → `.claude.vco-new/agents/coder.md`). A leaf-level
            #     `.vco-new` sibling would still sit INSIDE the symlinked
            #     directory and land bytes in the link's target — the bug
            #     F-1 fixes.
            redirect = _symlink_safe_redirect_target(dst)
            if redirect is not None:
                target = redirect

        data = src.read_bytes()
        atomic_write_bytes(target, data, fsync=fsync)
        if preserve_metadata:
            # F-2: match copy2's failure semantics — copy2 PROPAGATES a
            # copystat error, so we must NOT silently swallow it. Let it
            # flow to the outer handler below, which raises (default) or
            # routes through soft_fail/on_error. A silent pass here left the
            # dest at mkstemp's 0600 mode + src mtime un-applied with no
            # signal (partial-success drift). NOTE: the bytes are already
            # durably written by this point, so under soft_fail the dest is
            # a complete file with best-effort (possibly un-copied) metadata
            # — still safer than a truncated copy2.
            shutil.copystat(str(src), str(target))
        return target
    except (OSError, shutil.Error) as exc:
        if soft_fail:
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:  # noqa: BLE001 — logger must not re-raise
                    pass
            return None
        raise


def _symlink_safe_redirect_target(dst: Path) -> Optional[Path]:
    """Compute the V47-B ``.vco-new`` redirect target for ``dst`` when a
    symlink blocks the write, or ``None`` when no symlink is in the way.

    The NEW-8 convention (v0.2.53, originally inline in
    ``vco_lib.project_init._write_file_atomic``; since v0.2.92 that writer
    is a thin wrapper over :func:`atomic_write_bytes` with
    ``symlink_safe=True`` and THIS is the only copy of the walk) — the
    redirect happens at the SAME LEVEL the symlink lives, never at the leaf
    when the symlink is an ancestor:

      * ``dst`` itself is a symlink  → ``compute_vco_new_path(dst)``
        (leaf sibling — correct, the symlink is the leaf).
      * an ANCESTOR of ``dst`` is a symlink → ``.vco-new`` sibling of THAT
        ancestor with the path tail below it replicated. E.g. ``.claude``
        is a symlink and ``dst == .claude/agents/coder.md`` →
        ``.claude.vco-new/agents/coder.md``. This is the key correctness
        point of F-1: a leaf-level ``child.txt.vco-new`` would still resolve
        INSIDE the symlinked directory, so the bytes would land in the
        link's target — which the "hands-off symlinks" rule forbids.

    Returns the redirect target Path, or ``None`` when neither ``dst`` nor
    any ancestor is a symlink (the caller writes straight to ``dst``).
    """
    from vco_lib.symlink_handler import (  # noqa: PLC0415
        compute_vco_new_path,
        is_symlink_blocking,
    )

    # Case 1: dst itself is a symlink → leaf sibling (the symlink IS the leaf).
    if is_symlink_blocking(dst):
        return compute_vco_new_path(dst)

    # Case 2: walk ancestors; redirect at the FIRST symlinked ancestor,
    # replicating the path tail below it under the ancestor's .vco-new
    # sibling (exactly like project_init.py NEW-8).
    ancestor = dst.parent
    seen: set[str] = set()
    while True:
        ancestor_str = os.fspath(ancestor)
        if ancestor_str in seen:  # defensive: cyclic / root fixed point
            return None
        seen.add(ancestor_str)
        try:
            blocked = is_symlink_blocking(ancestor)
        except OSError:
            return None
        if blocked:
            vco_new_anc = compute_vco_new_path(ancestor)
            try:
                rel = dst.relative_to(ancestor)
            except ValueError:
                # Defensive: shouldn't happen on the ancestor walk; fall
                # back to the filename only.
                rel = Path(dst.name)
            return vco_new_anc / rel
        parent = ancestor.parent
        if parent == ancestor:  # reached filesystem root
            return None
        ancestor = parent


def atomic_write_json(
    path: Path,
    obj: Any,
    *,
    indent: Optional[int] = 2,
    sort_keys: bool = False,
    fsync: bool = True,
    mode: Optional[int] = None,
) -> None:
    """Serialize ``obj`` as JSON and write to ``path`` atomically.

    ``mode`` is applied to the final path after the rename (see
    :func:`atomic_write_text`); the sentinel / lock writers in
    ``vco_lib.collection_rename`` use it to keep claim files owner-only.
    """
    body = json.dumps(obj, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
    # Append a trailing newline for POSIX-friendly diffs.
    if not body.endswith("\n"):
        body = body + "\n"
    atomic_write_text(path, body, fsync=fsync, mode=mode)


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
