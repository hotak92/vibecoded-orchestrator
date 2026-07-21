# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for ``vco_lib.atomic.atomic_copy_file`` (pre-beta WP-E).

``atomic_copy_file`` is the shared replacement for install.py's raw
``shutil.copy2`` overwrite sites (the v0.2.81 step4d lesson: a raw copy2
truncate-and-writes at the FINAL path, so a mid-copy crash leaves a
truncated node the next step consumes). These tests pin:

* byte-identical content + copy2-style metadata preservation;
* the crash-safety invariant — a failure injected BETWEEN the tempfile
  write and the ``os.replace`` leaves the OLD destination intact and no
  ``.tmp`` leak (this is the whole reason the helper exists);
* ``soft_fail`` (swallow + return None, invoke ``on_error``) vs the
  default raise;
* the ``symlink_safe`` V47-B redirect (dest symlink AND symlinked
  ancestor → ``.vco-new`` sibling, original symlink untouched) vs the
  default False (behaviour-identical to copy2 at the already-gated
  install.py call sites).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from vco_lib.atomic import atomic_copy_file


def _make_symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(
            f"cannot create symlink on this platform: {exc} "
            "(Windows requires developer-mode or admin)"
        )


# ---------------------------------------------------------------------------
# Happy path: content + metadata
# ---------------------------------------------------------------------------

def test_copies_content_byte_identical(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"\x00\x01\x02payload\xff")
    dst = tmp_path / "dst.bin"

    returned = atomic_copy_file(src, dst)

    assert returned == dst
    assert dst.read_bytes() == b"\x00\x01\x02payload\xff"
    # No tempfile leak.
    assert list(tmp_path.glob("*.tmp")) == []


def test_overwrites_existing_destination(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("new content")
    dst = tmp_path / "dst.txt"
    dst.write_text("OLD content that must be replaced")

    atomic_copy_file(src, dst)

    assert dst.read_text() == "new content"


def test_preserves_metadata_like_copy2(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("x")
    # Give src a distinctive mtime + mode so copystat has something to move.
    os.utime(src, (1_600_000_000, 1_600_000_000))
    os.chmod(src, 0o640)
    dst = tmp_path / "dst.txt"

    atomic_copy_file(src, dst, preserve_metadata=True)

    st_src = src.stat()
    st_dst = dst.stat()
    assert int(st_dst.st_mtime) == int(st_src.st_mtime)
    # Mode bits (low 9) match — copystat semantics.
    assert (st_dst.st_mode & 0o777) == (st_src.st_mode & 0o777)


def test_preserve_metadata_false_skips_copystat(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("x")
    os.utime(src, (1_600_000_000, 1_600_000_000))
    dst = tmp_path / "dst.txt"

    atomic_copy_file(src, dst, preserve_metadata=False)

    # Content still copied; mtime NOT forced to src's (allow a tiny window).
    assert dst.read_text() == "x"
    assert abs(dst.stat().st_mtime - 1_600_000_000) > 1


# ---------------------------------------------------------------------------
# Crash-safety: failure injected between temp write and os.replace
# ---------------------------------------------------------------------------

def test_failure_before_replace_leaves_old_dest_intact(tmp_path, monkeypatch):
    """The core invariant: if os.replace fails (kill/power-loss window),
    the OLD destination must survive and no .tmp may leak — a plain copy2
    would already have truncated the dest by this point."""
    src = tmp_path / "src.txt"
    src.write_text("NEW never-lands")
    dst = tmp_path / "dst.txt"
    dst.write_text("OLD must survive")

    # Inject failure at the rename step (after the tempfile is fully written).
    real_replace = os.replace

    def boom(a, b):  # noqa: ANN001
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="injected replace failure"):
        atomic_copy_file(src, dst)

    monkeypatch.setattr(os, "replace", real_replace)
    # Old destination untouched; no partial/truncated write.
    assert dst.read_text() == "OLD must survive"
    # Tempfile cleaned up by atomic_write_bytes' except branch.
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# copystat failure semantics (F-2) — match copy2: propagate, never silent
# ---------------------------------------------------------------------------

def test_copystat_failure_propagates_by_default(tmp_path, monkeypatch):
    """F-2: copy2 PROPAGATES a copystat error; so must we. A copystat
    failure must NOT be silently swallowed (which would leave the dest at
    mkstemp's 0600 mode with no signal). The bytes are durably written by
    then, but the metadata failure is still surfaced by default."""
    src = tmp_path / "src.txt"
    src.write_text("payload")
    dst = tmp_path / "dst.txt"

    import shutil as _shutil

    def boom_copystat(a, b):  # noqa: ANN001
        raise OSError("injected copystat failure")

    monkeypatch.setattr(_shutil, "copystat", boom_copystat)

    with pytest.raises(OSError, match="injected copystat failure"):
        atomic_copy_file(src, dst, preserve_metadata=True)

    # Bytes were durably written before copystat ran (complete, not truncated).
    assert dst.read_text() == "payload"


def test_copystat_failure_routes_through_soft_fail(tmp_path, monkeypatch):
    """F-2: under soft_fail, a copystat failure is observable via on_error
    and returns None (not silently swallowed)."""
    src = tmp_path / "src.txt"
    src.write_text("payload")
    dst = tmp_path / "dst.txt"
    errors = []

    import shutil as _shutil

    def boom_copystat(a, b):  # noqa: ANN001
        raise OSError("injected copystat failure")

    monkeypatch.setattr(_shutil, "copystat", boom_copystat)

    result = atomic_copy_file(
        src, dst, preserve_metadata=True, soft_fail=True, on_error=errors.append
    )

    assert result is None
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)
    assert "injected copystat failure" in str(errors[0])


# ---------------------------------------------------------------------------
# soft_fail vs raise
# ---------------------------------------------------------------------------

def test_soft_fail_swallows_and_returns_none(tmp_path):
    src = tmp_path / "does-not-exist.bin"  # read_bytes → FileNotFoundError
    dst = tmp_path / "dst.bin"
    errors = []

    result = atomic_copy_file(
        src, dst, soft_fail=True, on_error=errors.append
    )

    assert result is None
    assert not dst.exists()
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)


def test_default_raises_on_error(tmp_path):
    src = tmp_path / "does-not-exist.bin"
    dst = tmp_path / "dst.bin"
    with pytest.raises(OSError):
        atomic_copy_file(src, dst)  # soft_fail defaults False


def test_soft_fail_on_error_logger_that_raises_is_tolerated(tmp_path):
    src = tmp_path / "nope.bin"
    dst = tmp_path / "dst.bin"

    def bad_logger(_exc):  # noqa: ANN001
        raise RuntimeError("logger blew up")

    # Must still return None, not propagate the logger's error.
    assert atomic_copy_file(src, dst, soft_fail=True, on_error=bad_logger) is None


# ---------------------------------------------------------------------------
# symlink_safe=False (default) — behaviour-identical to copy2 at gated sites
# ---------------------------------------------------------------------------

def test_symlink_safe_false_does_not_redirect(tmp_path):
    """With the default (symlink_safe=False), the helper writes straight to
    dst — matching the install.py adopt-project sites, which have already
    made their symlink decision upstream. It resolves+replaces at the leaf."""
    src = tmp_path / "src.txt"
    src.write_text("content")
    dst = tmp_path / "plain_dst.txt"

    returned = atomic_copy_file(src, dst)  # symlink_safe defaults False

    assert returned == dst
    assert dst.read_text() == "content"
    assert not (tmp_path / "plain_dst.txt.vco-new").exists()


# ---------------------------------------------------------------------------
# symlink_safe=True — V47-B redirect
# ---------------------------------------------------------------------------

def test_symlink_safe_redirects_when_dest_is_symlink(tmp_path):
    real_target = tmp_path / "real_target.txt"
    real_target.write_text("PRECIOUS unrelated content")
    dst = tmp_path / "dst.txt"
    _make_symlink_or_skip(real_target, dst)

    src = tmp_path / "src.txt"
    src.write_text("VCO content")

    returned = atomic_copy_file(src, dst, symlink_safe=True)

    # Landed at the .vco-new sibling, NOT through the symlink.
    assert returned == tmp_path / "dst.txt.vco-new"
    assert returned.read_text() == "VCO content"
    # The symlink itself is untouched and still points at the precious file.
    assert os.path.islink(dst)
    assert real_target.read_text() == "PRECIOUS unrelated content"


def test_symlink_safe_redirects_when_ancestor_is_symlink(tmp_path):
    """F-1: when an ANCESTOR of dst is a symlink, the redirect happens at
    the ancestor level (`.vco-new` sibling of the symlinked ancestor, tail
    replicated) — NEVER at the leaf, which would still resolve INSIDE the
    symlinked directory and land bytes in the link's target. Mirrors the
    NEW-8 convention at project_init._write_file_atomic:4540-4590."""
    # real_dir/ is the true directory; linked_dir -> real_dir.
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "keep.txt").write_text("must not be clobbered")
    linked_dir = tmp_path / "linked_dir"
    _make_symlink_or_skip(real_dir, linked_dir)

    src = tmp_path / "src.txt"
    src.write_text("VCO content")
    # Destination sits UNDER the symlinked ancestor.
    dst = linked_dir / "child.txt"

    returned = atomic_copy_file(src, dst, symlink_safe=True)

    # Redirected at the ANCESTOR level: `linked_dir.vco-new/child.txt`, a
    # brand-new real directory next to the symlink — the tail below the
    # symlinked ancestor is replicated.
    expected = tmp_path / "linked_dir.vco-new" / "child.txt"
    assert returned == expected
    assert returned.read_text() == "VCO content"

    # The correctness point of F-1: NOTHING was written through the symlink
    # into its target directory. real_dir keeps only what it started with.
    assert (real_dir / "keep.txt").read_text() == "must not be clobbered"
    assert not (real_dir / "child.txt").exists()
    assert not (real_dir / "child.txt.vco-new").exists()
    # The symlink itself is untouched (still a symlink pointing at real_dir).
    assert os.path.islink(linked_dir)
    # The redirect landed OUTSIDE the symlinked tree.
    assert (tmp_path / "linked_dir.vco-new").is_dir()
    assert not (tmp_path / "linked_dir.vco-new").is_symlink()


def test_symlink_safe_true_no_symlink_writes_directly(tmp_path):
    """symlink_safe=True but no symlink anywhere → writes straight to dst
    (no spurious .vco-new)."""
    src = tmp_path / "src.txt"
    src.write_text("content")
    dst = tmp_path / "sub" / "dst.txt"

    returned = atomic_copy_file(src, dst, symlink_safe=True)

    assert returned == dst
    assert dst.read_text() == "content"
    assert not Path(str(dst) + ".vco-new").exists()
