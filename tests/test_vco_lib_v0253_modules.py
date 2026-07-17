# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53: smoke tests for the new vco_lib modules.

Verifies the modules added per docs/INSTALL_ARCHITECTURE_v2.md §7:
* vco_lib.atomic     — atomic file writes (CORRECT-1 prep)
* vco_lib.hashing    — sha256 helpers
* vco_lib.settings_merge — settings.json merge (stub)
* vco_lib.timeutil   — UTC ISO-8601 helpers
* vco_lib.git_meta   — git HEAD / rev resolution

Stub modules verify only the import contract + interface.

(vco_lib.manifest — the sixth v0.2.53 skeleton — was DELETED in v0.2.75:
its three stubs only raised NotImplementedError with a "lands in v0.2.54"
promise that never materialised, and it accumulated zero production
callers. As of v0.2.85 (PLAN-v0285 WP-1) the SINGLE live manifest writer is
``project_init._write_manifest_atomic``: install.py's own
``_refresh_orchestrator_self_vco_manifest`` was DELETED when the root install
moved to the delegated ``install-bundle`` path, so there is now exactly one
manifest writer — the F-NEW-1 clobber-by-second-writer defect is gone by
construction.)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# vco_lib.atomic
# ---------------------------------------------------------------------------

def test_atomic_module_imports():
    from vco_lib import atomic
    assert hasattr(atomic, "atomic_write_text")
    assert hasattr(atomic, "atomic_write_bytes")
    assert hasattr(atomic, "atomic_write_json")


def test_atomic_write_text_happy_path(tmp_path):
    from vco_lib.atomic import atomic_write_text
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello world")
    assert target.read_text() == "hello world"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_text_cleans_up_on_failure(tmp_path):
    from vco_lib.atomic import atomic_write_text
    target = tmp_path / "out.txt"
    with patch("os.replace", side_effect=OSError("forced")):
        with pytest.raises(OSError):
            atomic_write_text(target, "hello")
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_bytes_writes_binary(tmp_path):
    from vco_lib.atomic import atomic_write_bytes
    target = tmp_path / "out.bin"
    atomic_write_bytes(target, b"\x00\x01\x02\x03")
    assert target.read_bytes() == b"\x00\x01\x02\x03"


def test_atomic_write_json_writes_valid_json(tmp_path):
    from vco_lib.atomic import atomic_write_json
    target = tmp_path / "out.json"
    atomic_write_json(target, {"foo": "bar", "list": [1, 2, 3]})
    parsed = json.loads(target.read_text())
    assert parsed == {"foo": "bar", "list": [1, 2, 3]}


def test_atomic_write_json_has_trailing_newline(tmp_path):
    from vco_lib.atomic import atomic_write_json
    target = tmp_path / "out.json"
    atomic_write_json(target, {})
    assert target.read_text().endswith("\n")


# ---------------------------------------------------------------------------
# vco_lib.hashing
# ---------------------------------------------------------------------------

def test_hashing_module_imports():
    from vco_lib import hashing
    assert hasattr(hashing, "sha256_file")
    assert hasattr(hashing, "sha256_bytes")
    assert hasattr(hashing, "sha256_text")


def test_sha256_bytes_known_vector():
    from vco_lib.hashing import sha256_bytes
    # sha256("") = e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    assert sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_sha256_text_matches_bytes_after_encode():
    from vco_lib.hashing import sha256_bytes, sha256_text
    assert sha256_text("hello") == sha256_bytes(b"hello")


def test_sha256_file_matches_bytes(tmp_path):
    from vco_lib.hashing import sha256_bytes, sha256_file
    target = tmp_path / "f.txt"
    target.write_text("hello world")
    assert sha256_file(target) == sha256_bytes(b"hello world")


# ---------------------------------------------------------------------------
# vco_lib.settings_merge (stub)
# ---------------------------------------------------------------------------

def test_settings_merge_module_imports():
    from vco_lib import settings_merge
    assert hasattr(settings_merge, "merge_settings_template")
    assert hasattr(settings_merge, "SettingsMergeResult")


def test_settings_merge_template_stub_raises(tmp_path):
    """v0.2.53 stub: calling the function raises NotImplementedError."""
    from vco_lib.settings_merge import merge_settings_template
    with pytest.raises(NotImplementedError):
        merge_settings_template(tmp_path / "settings.json", {})


# ---------------------------------------------------------------------------
# vco_lib.timeutil
# ---------------------------------------------------------------------------

def test_timeutil_module_imports():
    from vco_lib import timeutil
    assert hasattr(timeutil, "utc_iso_now")
    assert hasattr(timeutil, "utc_iso_now_us")


def test_utc_iso_now_format():
    from vco_lib.timeutil import utc_iso_now
    ts = utc_iso_now()
    # Format: YYYY-MM-DDTHH:MM:SSZ
    assert ts.endswith("Z")
    assert len(ts) == 20  # YYYY-MM-DDTHH:MM:SSZ
    # No fractional seconds.
    assert "." not in ts


def test_utc_iso_now_us_has_microseconds():
    from vco_lib.timeutil import utc_iso_now_us
    ts = utc_iso_now_us()
    # Format: YYYY-MM-DDTHH:MM:SS.ffffff+00:00
    assert "+00:00" in ts
    assert "." in ts


# ---------------------------------------------------------------------------
# vco_lib.git_meta
# ---------------------------------------------------------------------------

def test_git_meta_module_imports():
    from vco_lib import git_meta
    assert hasattr(git_meta, "resolve_vco_version")
    assert hasattr(git_meta, "git_short_sha")
    assert hasattr(git_meta, "git_branch")


def test_resolve_vco_version_reads_version_file(tmp_path):
    from vco_lib.git_meta import resolve_vco_version
    (tmp_path / "VERSION").write_text("0.2.53")
    assert resolve_vco_version(tmp_path) == "v0.2.53"


def test_resolve_vco_version_keeps_v_prefix_when_present(tmp_path):
    from vco_lib.git_meta import resolve_vco_version
    (tmp_path / "VERSION").write_text("v0.2.53")
    assert resolve_vco_version(tmp_path) == "v0.2.53"


def test_resolve_vco_version_falls_back_to_unknown(tmp_path):
    from vco_lib.git_meta import resolve_vco_version
    # No VERSION, no .git → "unknown".
    assert resolve_vco_version(tmp_path) == "unknown"


def test_git_short_sha_returns_none_for_non_git_dir(tmp_path):
    from vco_lib.git_meta import git_short_sha
    assert git_short_sha(tmp_path) is None


def test_git_branch_returns_none_for_non_git_dir(tmp_path):
    from vco_lib.git_meta import git_branch
    assert git_branch(tmp_path) is None


def test_git_short_sha_on_real_repo():
    """The worktree itself is a real git repo; resolve its short SHA."""
    from vco_lib.git_meta import git_short_sha
    sha = git_short_sha(REPO_ROOT)
    if sha is None:
        pytest.skip("worktree is not a git repo (CI tarball?)")
    # Short SHAs are 7-12 hex chars.
    assert 7 <= len(sha) <= 12
    assert all(c in "0123456789abcdef" for c in sha)
