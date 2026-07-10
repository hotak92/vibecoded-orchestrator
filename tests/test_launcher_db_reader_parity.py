# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 DEDUP-4 / CORRECT-2: launcher.db readonly migration.

Verifies that install.py's _read_app_state_key now routes through
vco_lib.launcher_db_reader._open_db_readonly (mode=ro) rather than
the blocking sqlite3.connect(timeout=5.0).

Pre-v0.2.53 the inline pattern could block install.py for up to 5s
on Windows when the launcher held a write lock — a perceived
install-hang during a performance-critical step (CORRECT-2).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"


@pytest.fixture(scope="module")
def install_module():
    spec = importlib.util.spec_from_file_location("install_under_test_d4", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install_under_test_d4"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def launcher_db(tmp_path, monkeypatch):
    """Create a minimal launcher.db with app_state table for tests."""
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE app_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
        ("test.key", "test_value", int(time.time() * 1000)),
    )
    conn.commit()
    conn.close()

    # Point both install.py and launcher_db_reader at the temp DB via
    # the VCT_STATE_DIR env var (canonical override).
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    # Also override VCT_LAUNCHER_DB_PATH for launcher_db_reader's
    # direct path override (which takes precedence over vct_root_dir).
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    return db


def test_read_app_state_key_reads_existing_value(install_module, launcher_db):
    """The migrated _read_app_state_key reads existing values."""
    value = install_module._read_app_state_key("test.key")
    assert value == "test_value"


def test_read_app_state_key_returns_none_for_missing(install_module, launcher_db):
    """Missing keys return None (soft-fail)."""
    assert install_module._read_app_state_key("nonexistent.key") is None


def test_read_app_state_key_returns_none_for_missing_db(install_module, tmp_path, monkeypatch):
    """Missing launcher.db returns None (free-tier / pre-launcher install)."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.delenv("VCT_LAUNCHER_DB_PATH", raising=False)
    assert install_module._read_app_state_key("any.key") is None


def test_read_app_state_key_uses_readonly_uri(install_module):
    """The readonly mode=ro URI form is used for app_state reads.

    Regression guard: a future edit that re-introduces the blocking
    sqlite3.connect (without uri=True + mode=ro) would re-introduce
    CORRECT-2.

    v0.2.77 Part 7a cluster D: the canonical read implementation moved from
    install.py's inline ``_read_app_state_key`` body into
    ``vco_lib.launcher_db_writer.read_app_state_key`` (its own module so the
    read-only ``launcher_db_reader`` never reaches a writer). This guard now
    scans that home for the readonly-URI pattern, AND still asserts install.py
    itself (whose ``_read_app_state_key`` is now a thin delegator) does not
    re-introduce the legacy blocking pattern.
    """
    writer_src = (REPO_ROOT / "vco_lib" / "launcher_db_writer.py").read_text(
        encoding="utf-8"
    )
    func_start = writer_src.find("def read_app_state_key(")
    assert func_start > 0, "launcher_db_writer.read_app_state_key not found"
    func_end = writer_src.find("\ndef ", func_start + 1)
    body = writer_src[func_start:func_end]
    # Body MUST use mode=ro URI form (the readonly + immutable pattern).
    assert "mode=ro" in body, (
        "read_app_state_key must use the `file:?mode=ro&immutable=1` "
        "URI form for non-blocking access (CORRECT-2)."
    )
    assert "uri=True" in body, (
        "sqlite3.connect must be called with uri=True to honor the "
        "mode=ro URI form."
    )
    # The OLD blocking pattern (just timeout=5.0 without URI) must NOT appear
    # in the read path — neither in the canonical home nor re-introduced in
    # install.py's delegating wrapper.
    assert "sqlite3.connect(str(db_path), timeout=5.0)" not in body, (
        "read_app_state_key must NOT use the legacy blocking pattern."
    )
    install_src = INSTALL_PY.read_text(encoding="utf-8")
    read_start = install_src.find("def _read_app_state_key(")
    read_end = install_src.find("\ndef ", read_start + 1)
    read_body = install_src[read_start:read_end]
    assert "sqlite3.connect(str(db_path), timeout=5.0)" not in read_body, (
        "install._read_app_state_key must NOT re-introduce the legacy "
        "blocking pattern (it now delegates to launcher_db_writer)."
    )


def test_launcher_db_reader_read_app_state_value_uses_readonly(install_module):
    """vco_lib.launcher_db_reader.read_app_state_value uses mode=ro URI."""
    src = (REPO_ROOT / "vco_lib" / "launcher_db_reader.py").read_text(
        encoding="utf-8"
    )
    # Either _open_db_readonly's `mode=ro` URI or the value-reader uses it.
    assert "mode=ro" in src, (
        "launcher_db_reader must use mode=ro URI form for non-blocking access."
    )


def test_no_blocking_sqlite_connect_with_5s_timeout_for_read_only_app_state(install_module):
    """The specific pattern `sqlite3.connect(str(db_path), timeout=5.0)` for
    app_state read-only access must not appear in install.py for the
    _read_app_state_key code path.

    This is a structural check focused on the read-only app_state
    sites — write sites (`_write_preset_defaults_to_app_state` and the
    rl_reranker default writer) legitimately need timeout-based RW
    connections and are out of scope.
    """
    src = INSTALL_PY.read_text(encoding="utf-8")
    # _read_app_state_key's body must not contain the blocking pattern.
    func_start = src.find("def _read_app_state_key(")
    func_end = src.find("\ndef ", func_start + 1)
    body = src[func_start:func_end]
    assert "sqlite3.connect(str(db_path), timeout=5.0)" not in body
