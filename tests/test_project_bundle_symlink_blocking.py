# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""NEW-8 / B3 (v0.2.53) — per-project ``_write_file_atomic`` symlink-blocking.

Mirrors the orchestrator-self V47-B defense (install.py:1286) for the
per-project ``vco_lib.project_init._write_file_atomic`` callers. Before
this fix, the per-project path did plain tempfile + os.replace, which
on POSIX would replace the SYMLINK TARGET (silent data destruction).
Worse: a symlinked ``.claude/`` directory would have files written to
the target of the symlink (= unrelated location).

Audit:
  ``.claude/context/audits/project-bundle-install-audit-2026-06-10.md``
  §6.7 / B3.

Test cases:
  1. Direct symlink target: writing to a path that's itself a symlink
     redirects to the ``.vco-new`` sibling.
  2. Ancestor symlink: writing to a path under a symlinked directory
     (e.g. user symlinked ``<project>/.claude``) redirects to the
     ``.vco-new`` sibling of the symlinked ancestor.
  3. Regular write (no symlinks anywhere) — control case, unchanged
     behaviour.
  4. Dangling symlink target — treated the same as a live symlink:
     refuse to write through.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from vco_lib.project_init import _write_file_atomic


@pytest.fixture
def tmp_root() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="vct-symlink-block-test-"))
    yield folder
    # Clean up; be careful with symlinks.
    import shutil

    shutil.rmtree(folder, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────
# Case 1: target itself is a symlink
# ──────────────────────────────────────────────────────────────────────


class TestDirectSymlinkTarget:
    def test_symlink_file_target_redirects_to_vco_new(self, tmp_root: Path) -> None:
        """Writing to ``<root>/coder.md`` when that path is a symlink to
        an unrelated file must NOT overwrite the symlink target. The new
        content must land at ``<root>/coder.md.vco-new``."""
        unrelated_target = tmp_root / "unrelated.md"
        unrelated_target.write_text("ORIGINAL UNRELATED CONTENT\n", encoding="utf-8")

        symlink_path = tmp_root / "coder.md"
        os.symlink(unrelated_target, symlink_path)
        assert symlink_path.is_symlink()

        new_content = b"new content from VCO\n"
        _write_file_atomic(symlink_path, new_content)

        # Symlink still in place, pointing at the original.
        assert symlink_path.is_symlink()
        # Unrelated file UNTOUCHED — this is the load-bearing assertion.
        assert unrelated_target.read_text(encoding="utf-8") == "ORIGINAL UNRELATED CONTENT\n"
        # New content landed at the .vco-new sibling.
        vco_new = tmp_root / "coder.md.vco-new"
        assert vco_new.exists()
        assert vco_new.read_bytes() == new_content

    def test_symlink_dir_target_redirects_to_vco_new(self, tmp_root: Path) -> None:
        """Writing into a path that flows through a directory-symlink
        gets redirected too. This is the highest-impact case from the
        audit: a user symlinks ``<project>/.claude`` to a shared
        location, then runs ``install-bundle --update`` — every file
        VCO would write into ``.claude/`` would silently land in the
        symlink's destination."""
        # Make a real directory elsewhere that VCO must NOT touch.
        shared = tmp_root / "shared-dir"
        shared.mkdir()
        (shared / "user-edit.md").write_text("USER OWNS THIS\n", encoding="utf-8")

        # Symlink the project's .claude dir to the shared location.
        project_claude = tmp_root / "project" / ".claude"
        project_claude.parent.mkdir(parents=True)
        os.symlink(shared, project_claude)
        assert project_claude.is_symlink()

        # VCO tries to write `.claude/agents/coder.md`.
        target = project_claude / "agents" / "coder.md"
        new_content = b"VCO shipped content\n"
        _write_file_atomic(target, new_content)

        # The shared dir is UNTOUCHED.
        assert (shared / "user-edit.md").read_text(encoding="utf-8") == "USER OWNS THIS\n"
        assert not (shared / "agents").exists(), (
            "VCO must NOT have written into the symlink target"
        )
        # .vco-new sibling holds the content.
        vco_new_claude = tmp_root / "project" / ".claude.vco-new"
        assert (vco_new_claude / "agents" / "coder.md").exists()
        assert (vco_new_claude / "agents" / "coder.md").read_bytes() == new_content


# ──────────────────────────────────────────────────────────────────────
# Case 2: regular write (no symlinks) — control
# ──────────────────────────────────────────────────────────────────────


class TestRegularWriteUnchanged:
    def test_normal_write_works_as_before(self, tmp_root: Path) -> None:
        """No symlinks anywhere → behaviour unchanged: tempfile + os.replace
        lands at the requested target."""
        target = tmp_root / "subdir" / "file.txt"
        new_content = b"hello\n"
        _write_file_atomic(target, new_content)

        assert target.exists()
        assert target.read_bytes() == new_content
        # No .vco-new sibling created.
        vco_new = tmp_root / "subdir" / "file.txt.vco-new"
        assert not vco_new.exists()

    def test_mode_bits_preserved(self, tmp_root: Path) -> None:
        """The mode kwarg still applies on normal writes (regression
        guard — NEW-8 must not break the existing executable-bit
        handling for shell scripts)."""
        target = tmp_root / "script.sh"
        _write_file_atomic(target, b"#!/bin/sh\necho hi\n", mode=0o755)
        assert target.exists()
        # On POSIX, the mode bits should be 0o755. On Windows, chmod is
        # a no-op; just assert the file exists.
        if sys.platform != "win32":
            assert os.stat(target).st_mode & 0o777 == 0o755


# ──────────────────────────────────────────────────────────────────────
# Case 3: dangling symlink — treated as live symlink (still blocking)
# ──────────────────────────────────────────────────────────────────────


class TestDanglingSymlinkTarget:
    def test_dangling_symlink_redirects_to_vco_new(self, tmp_root: Path) -> None:
        """A symlink pointing at a path that doesn't exist is still a
        symlink — ``os.path.islink`` returns True. We must STILL
        refuse to write through it (writing through a dangling symlink
        on POSIX creates the destination, which may be in an unrelated
        location)."""
        nonexistent_dest = tmp_root / "does-not-exist.md"
        symlink_path = tmp_root / "coder.md"
        os.symlink(nonexistent_dest, symlink_path)
        assert symlink_path.is_symlink()
        assert not symlink_path.exists()  # dangling

        new_content = b"VCO content\n"
        _write_file_atomic(symlink_path, new_content)

        # Symlink still dangling.
        assert symlink_path.is_symlink()
        assert not nonexistent_dest.exists(), (
            "VCO must NOT have created the dangling-symlink destination"
        )
        # New content at .vco-new.
        vco_new = tmp_root / "coder.md.vco-new"
        assert vco_new.exists()
        assert vco_new.read_bytes() == new_content


# ──────────────────────────────────────────────────────────────────────
# Case 4: nested redirects — the .vco-new sibling itself can be a
# regular dir; subsequent writes against the same project go there
# ──────────────────────────────────────────────────────────────────────


class TestRedirectIsPersistent:
    def test_second_write_under_same_symlinked_dir_redirects_again(
        self, tmp_root: Path
    ) -> None:
        """Two writes to different files inside the same symlinked
        directory each redirect independently — the .vco-new tree
        accumulates correctly."""
        shared = tmp_root / "shared-dir"
        shared.mkdir()

        project_claude = tmp_root / "project" / ".claude"
        project_claude.parent.mkdir(parents=True)
        os.symlink(shared, project_claude)

        _write_file_atomic(
            project_claude / "agents" / "a.md", b"agent A\n"
        )
        _write_file_atomic(
            project_claude / "skills" / "tdd" / "SKILL.md", b"skill TDD\n"
        )

        # Both writes ended up in the .vco-new sibling.
        vco_new = tmp_root / "project" / ".claude.vco-new"
        assert (vco_new / "agents" / "a.md").read_bytes() == b"agent A\n"
        assert (vco_new / "skills" / "tdd" / "SKILL.md").read_bytes() == b"skill TDD\n"
        # Shared dir UNTOUCHED.
        assert not (shared / "agents").exists()
        assert not (shared / "skills").exists()
