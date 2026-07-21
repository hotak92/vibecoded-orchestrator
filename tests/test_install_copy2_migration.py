# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py copy2 → atomic_copy_file migration (pre-beta WP-E).

v0.2.81 (KG: step4d-write-consolidation) found install.py hand-inlining a
weaker, non-atomic copy instead of the shared primitive, and flagged the
remaining raw ``shutil.copy2`` overwrite sites as the same pre-existing
pattern. WP-E routed the live-overwrite copy2 sites through
``vco_lib.atomic.atomic_copy_file``.

These tests pin:

1. The migrated adopt-project copy paths (``_copy_recursive`` /
   ``_copy_recursive_preserve``) still land file content BYTE-IDENTICAL to
   the source — the migration is behaviour-preserving, not just green.
2. The migrated overwrite path is now crash-atomic: an ``os.replace``
   failure mid-copy leaves the OLD destination intact (a plain copy2 would
   have truncated it). This is the property WP-E added.
3. The copy2 site count stays at-or-below the post-migration ratchet, and
   the deleted live-overwrite sites don't silently regrow.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_INSTALL_PY = REPO_ROOT / "install.py"

# Lazy-load install.py the same way the V47 tests do.
_spec = importlib.util.spec_from_file_location("install_py_wpe_copy2", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_wpe_copy2"] = install_py
_spec.loader.exec_module(install_py)


# ---------------------------------------------------------------------------
# 1. byte-identical content through the migrated paths
# ---------------------------------------------------------------------------

def test_copy_recursive_lands_byte_identical(tmp_path):
    src_root = tmp_path / "src"
    (src_root / "sub").mkdir(parents=True)
    payload_a = b"\x00\x01binary\xffpayload"
    payload_b = "nested text node\n"
    (src_root / "a.bin").write_bytes(payload_a)
    (src_root / "sub" / "b.md").write_text(payload_b)

    dst_root = tmp_path / "dst"
    copied = install_py._copy_recursive(src_root, dst_root)

    assert copied == 2
    assert (dst_root / "a.bin").read_bytes() == payload_a
    assert (dst_root / "sub" / "b.md").read_text() == payload_b


def test_copy_recursive_preserve_overwrite_lands_byte_identical(tmp_path):
    """The migrated site 2529: plain (non-symlink, non-preserved) overwrite
    of a real destination — the exact copy2 the v0.2.81 lesson targeted."""
    install_root = tmp_path / "install"
    install_root.mkdir()
    src = tmp_path / "src" / "config.json"
    src.parent.mkdir()
    src.write_bytes(b'{"key": "NEW value"}')

    dst = install_root / "config.json"
    dst.write_bytes(b'{"key": "OLD value"}')  # pre-existing real file

    preserved_present: list[str] = []
    visited, new = install_py._copy_recursive_preserve(
        src, dst, install_root, preserve=[], preserved_present=preserved_present,
    )

    assert visited == 1
    assert new == 0  # plain overwrite, not a .new sibling
    assert dst.read_bytes() == b'{"key": "NEW value"}'
    assert preserved_present == []


# ---------------------------------------------------------------------------
# 2. crash-atomicity: the property WP-E added over raw copy2
# ---------------------------------------------------------------------------

def test_migrated_overwrite_is_crash_atomic(tmp_path, monkeypatch):
    """Inject an os.replace failure during the migrated overwrite. A raw
    copy2 would already have truncated the destination; the atomic helper
    must leave the OLD content intact and leak no tempfile."""
    install_root = tmp_path / "install"
    install_root.mkdir()
    src = tmp_path / "src.txt"
    src.write_text("NEW never-lands")
    dst = install_root / "dst.txt"
    dst.write_text("OLD must survive")

    def boom(a, b):  # noqa: ANN001
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="injected replace failure"):
        install_py._copy_recursive_preserve(
            src, dst, install_root, preserve=[], preserved_present=[],
        )

    monkeypatch.undo()
    # Old destination survived; no truncated write, no .tmp leak.
    assert dst.read_text() == "OLD must survive"
    assert list(install_root.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 3. structural: migrated sites use the shared helper; count within ratchet
# ---------------------------------------------------------------------------

def test_adopt_project_sites_use_shared_helper():
    src = _INSTALL_PY.read_text(encoding="utf-8")
    # The four adopt-project byte-copies are routed through the shared helper.
    assert "_atomic_copy_file(src, dst)" in src
    assert "_atomic_copy_file(src, vco_new)" in src
    assert "_atomic_copy_file(src, sibling)" in src
    # And the import is present.
    assert "from vco_lib.atomic import atomic_copy_file" in src


def test_copy2_call_count_within_post_migration_ratchet():
    src = _INSTALL_PY.read_text(encoding="utf-8")
    n = src.count("shutil.copy2(")
    # After WP-E: the `shutil.copy2(` STRING count is 8 — 7 real call sites
    # (4 dist-binary-swap-dance + 3 fresh-.bak backups, all with per-call
    # justification comments) PLUS 1 comment mention that also matches the
    # grep string. This is a ratchet — it may only DROP.
    assert n <= 8, (
        f"install.py has {n} `shutil.copy2(` occurrences (string count); WP-E "
        "left 7 real KEPT calls + 1 comment mention = 8 (dist-swap dance + "
        "fresh .bak backups). New copy2 overwrite sites must go through "
        "vco_lib.atomic.atomic_copy_file instead."
    )
