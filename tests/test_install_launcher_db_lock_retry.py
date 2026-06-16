"""v0.2.60 — `_connect_launcher_db_with_retry` (launcher-self-db-lock fix).

The launcher closes its managed launcher.db connection for the install.py
window and stands the fresh-conn pollers down, so the writer lock should be
free — but on Windows an antivirus/indexer can briefly retain the lock.
This helper rides out those transients with bounded retry-with-backoff
instead of failing straight into a `kg_binding_self_heal_db_error` deferral.
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_install_module():
    """Import install.py as a module (it's a top-level script, not a pkg)."""
    if "install" in sys.modules:
        return sys.modules["install"]
    spec = importlib.util.spec_from_file_location("install", REPO_ROOT / "install.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["install"] = mod
    spec.loader.exec_module(mod)
    return mod


install = _load_install_module()


def test_connect_succeeds_first_try(tmp_path):
    db = tmp_path / "launcher.db"
    sqlite3.connect(str(db)).close()  # create the file
    conn = install._connect_launcher_db_with_retry(db, attempts=3, base_delay=0.0)
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_connect_retries_then_succeeds(tmp_path, monkeypatch):
    """First N-1 attempts raise 'database is locked', last succeeds."""
    db = tmp_path / "launcher.db"
    sqlite3.connect(str(db)).close()

    real_connect = sqlite3.connect
    calls = {"n": 0}

    def flaky_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", flaky_connect)
    # base_delay=0 so the test doesn't actually sleep.
    conn = install._connect_launcher_db_with_retry(db, attempts=5, base_delay=0.0)
    try:
        assert calls["n"] == 3  # two failures + one success
    finally:
        conn.close()


def test_connect_reraises_after_exhaustion(tmp_path, monkeypatch):
    """All attempts locked → re-raise so the caller's deferral fires."""
    db = tmp_path / "launcher.db"

    def always_locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", always_locked)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        install._connect_launcher_db_with_retry(db, attempts=3, base_delay=0.0)


def test_connect_does_not_retry_non_lock_errors(tmp_path, monkeypatch):
    """A corruption/permission error must fail FAST (no pointless retries)."""
    db = tmp_path / "launcher.db"
    calls = {"n": 0}

    def corrupt(*args, **kwargs):
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlite3, "connect", corrupt)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        install._connect_launcher_db_with_retry(db, attempts=5, base_delay=0.0)
    assert calls["n"] == 1, "non-lock errors must not be retried"
