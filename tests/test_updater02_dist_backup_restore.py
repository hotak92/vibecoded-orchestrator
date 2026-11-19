# SPDX-License-Identifier: AGPL-3.0-or-later
"""UPDATER-02: a binary left only as ``<name>.old-<pid>`` by an interrupted
update is put back at its canonical path — and nothing else is touched.

Covers the Python half (``vco_lib.dist_binary_repair.restore_missing_from_backups``,
its use in ``vco_lib.hub_ensure.find_dist_binary`` — the session-start hooks'
resolver — and the ``restore-backups`` CLI that ``start-launcher.bat`` runs).
The Rust boot sweep's mirror is tested in ``lib.rs::boot_sweep_tests``.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vco_lib import dist_binary_repair as dbr
from vco_lib import hub_ensure

DEAD = lambda _pid: False  # noqa: E731
ALIVE = lambda _pid: True  # noqa: E731


def _write_exe(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_decision_table():
    assert dbr.decide_backup_restore(canonical_exists=False, backup_pid_alive=False)
    assert not dbr.decide_backup_restore(canonical_exists=True, backup_pid_alive=False)
    assert not dbr.decide_backup_restore(canonical_exists=False, backup_pid_alive=True)
    assert not dbr.decide_backup_restore(canonical_exists=True, backup_pid_alive=True)


def test_missing_canonical_is_restored_from_the_newest_dead_backup(tmp_path):
    older = tmp_path / "vct-hub.exe.old-111"
    newer = tmp_path / "vct-hub.exe.old-222"
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")
    past = time.time() - 3600
    os.utime(older, (past, past))

    restored = dbr.restore_missing_from_backups(tmp_path, ["vct-hub.exe"], pid_alive=DEAD)

    assert restored == [tmp_path / "vct-hub.exe"]
    assert (tmp_path / "vct-hub.exe").read_bytes() == b"newer"
    assert not newer.exists()
    assert older.exists(), "only one backup is consumed; the boot sweep collects the rest"


def test_present_canonical_is_never_overwritten(tmp_path):
    canonical = tmp_path / "vct-launcher.exe"
    canonical.write_bytes(b"current")
    backup = tmp_path / "vct-launcher.exe.old-333"
    backup.write_bytes(b"stale")

    assert dbr.restore_missing_from_backups(tmp_path, ["vct-launcher.exe"], pid_alive=DEAD) == []
    assert canonical.read_bytes() == b"current"
    assert backup.read_bytes() == b"stale"


def test_live_pid_backup_is_left_for_the_running_update(tmp_path):
    backup = tmp_path / "vct-launcher.exe.old-444"
    backup.write_bytes(b"running image")

    assert dbr.restore_missing_from_backups(tmp_path, ["vct-launcher.exe"], pid_alive=ALIVE) == []
    assert not (tmp_path / "vct-launcher.exe").exists()
    assert backup.exists()


def test_foreign_names_are_never_matched(tmp_path):
    # A user's own copy and a non-numeric suffix are not update backups.
    (tmp_path / "vct-launcher.exe.old-may7").write_bytes(b"x")
    (tmp_path / "vct-launcher-marker.exe.old-12").write_bytes(b"x")
    assert dbr.restore_missing_from_backups(tmp_path, ["vct-launcher.exe"], pid_alive=DEAD) == []
    assert not (tmp_path / "vct-launcher.exe").exists()


def test_unreadable_directory_restores_nothing(tmp_path):
    assert dbr.restore_missing_from_backups(tmp_path / "absent", ["x"], pid_alive=DEAD) == []


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """An environment in which step 2 probes only the fixture root."""
    env = {"HOME": str(tmp_path / "home"), "PATH": ""}
    monkeypatch.setattr(hub_ensure, "_orchestrator_roots", lambda _environ: [])
    monkeypatch.setattr(hub_ensure.shutil, "which", lambda _name: None)
    return env


def test_hub_resolver_restores_a_backup_when_the_dist_slot_is_empty(tmp_path, isolated_env):
    arch = hub_ensure.dist_arch_dir()
    assert arch, "test host must map to a dist slot"
    slot = tmp_path / "launcher" / "dist" / arch
    slot.mkdir(parents=True)
    name = hub_ensure.hub_binary_names()[0]
    # i32::MAX: never an allocatable pid on any supported OS.
    _write_exe(slot / f"{name}.old-{2**31 - 1}", b"hub bytes")

    found = hub_ensure.find_hub_binary(repo_root=tmp_path, env=isolated_env)

    assert found == slot / name
    assert (slot / name).read_bytes() == b"hub bytes"


def test_hub_resolver_leaves_a_present_binary_and_its_backup_alone(tmp_path, isolated_env):
    arch = hub_ensure.dist_arch_dir()
    slot = tmp_path / "launcher" / "dist" / arch
    slot.mkdir(parents=True)
    name = hub_ensure.hub_binary_names()[0]
    _write_exe(slot / name, b"current")
    backup = slot / f"{name}.old-{2**31 - 1}"
    _write_exe(backup, b"old")

    assert hub_ensure.find_hub_binary(repo_root=tmp_path, env=isolated_env) == slot / name
    assert (slot / name).read_bytes() == b"current"
    assert backup.exists()


def test_cli_restore_backups(tmp_path):
    backup = tmp_path / f"vct-launcher.exe.old-{2**31 - 1}"
    backup.write_bytes(b"bytes")
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            sys.executable, "-m", "vco_lib.dist_binary_repair", "restore-backups",
            "--dir", str(tmp_path), "--name", "vct-launcher.exe", "--name", "vct-hub.exe",
        ],
        cwd=repo, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(repo)},
    )
    assert proc.returncode == 0, proc.stderr
    assert "restored" in proc.stdout
    assert (tmp_path / "vct-launcher.exe").read_bytes() == b"bytes"
