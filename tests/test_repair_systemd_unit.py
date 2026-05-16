# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for `_repair_systemd_unit_working_dir` (PR-12 v0.2.11 Bug C).

The repair helper is the auto-fix for stale `WorkingDirectory=` lines in
the systemd user unit. It runs INSIDE `_materialize_boot_service` on the
update path, BEFORE the renderer, so a unit pinned to a stale path from
a prior install gets re-pointed at the current install location.

Coverage matrix:

  - Idempotent no-op when WorkingDirectory= already matches (no read
    churn, no log spam, no deferral entry, no backup file).
  - Repair landed: backup written, unit rewritten, deferral entry added,
    return value carries (old, new) tuple.
  - Soft-fail when unit file is missing.
  - Soft-fail when unit has no WorkingDirectory= line at all.
  - Soft-fail on non-Linux OS.
  - In-place edit preserves user customisations to OTHER lines.
  - Environment=VCT_STACK_WORKING_DIR= line is updated in lockstep with
    WorkingDirectory= so the wrapper script sees the new path too.

Hermetic — every test runs against tmp_path with monkeypatched Path.home()
and platform.system(), no real systemd touched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect Path.home() to a tmp dir for the duration of the test."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def force_linux(monkeypatch) -> None:
    """Force platform.system() to return 'Linux' so the helper doesn't
    short-circuit on non-Linux test hosts."""
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")


def _write_unit(home: Path, working_dir: str,
                env_working_dir: str | None = None,
                extra_lines: str = "") -> Path:
    """Materialise a fake systemd unit at the canonical user path."""
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_path = unit_dir / install._BOOT_SERVICE_UNIT_NAME
    env_line = ""
    if env_working_dir is not None:
        env_line = f"Environment=VCT_STACK_WORKING_DIR={env_working_dir}\n"
    unit_path.write_text(
        "[Unit]\n"
        "Description=Claude MCP containers\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={working_dir}\n"
        f"{env_line}"
        "ExecStart=/usr/bin/env bash /opt/wrapper.sh\n"
        f"{extra_lines}"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    return unit_path


# ---------------------------------------------------------------------------
# Idempotency — no-op when already correct
# ---------------------------------------------------------------------------


def test_no_op_when_working_dir_already_correct(fake_home: Path, force_linux,
                                                 tmp_path: Path):
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    correct = str(cms.resolve())
    unit_path = _write_unit(fake_home, working_dir=correct,
                            env_working_dir=correct)
    mtime_before = unit_path.stat().st_mtime_ns

    deferral = DeferralReport()
    result = install._repair_systemd_unit_working_dir(
        install_path=install_path,
        deferral_report=deferral,
    )
    # No repair landed.
    assert result is None
    # File untouched (mtime preserved — proves we didn't rewrite).
    assert unit_path.stat().st_mtime_ns == mtime_before
    # No backup file created.
    backups = list(unit_path.parent.glob(f"{unit_path.name}.bak-*"))
    assert backups == []
    # No deferral entries.
    assert deferral.entries == []


def test_no_op_when_unit_missing(fake_home: Path, force_linux, tmp_path: Path):
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()
    # Unit file absent — _write_unit not called.

    deferral = DeferralReport()
    result = install._repair_systemd_unit_working_dir(
        install_path=install_path,
        deferral_report=deferral,
    )
    assert result is None
    assert deferral.entries == []


def test_no_op_on_non_linux(fake_home: Path, monkeypatch, tmp_path: Path):
    """Repair is a Linux-only feature (systemd user units don't exist
    on macOS/Windows)."""
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()
    # Even with a stale unit on disk, the helper must short-circuit.
    _write_unit(fake_home, working_dir="/totally/stale/path")

    result = install._repair_systemd_unit_working_dir(install_path=install_path)
    assert result is None


# ---------------------------------------------------------------------------
# Repair landed
# ---------------------------------------------------------------------------


def test_repair_rewrites_stale_working_dir(fake_home: Path, force_linux,
                                           tmp_path: Path):
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    correct = str(cms.resolve())
    stale = "/old/install/claude_mcp_servers"
    unit_path = _write_unit(fake_home, working_dir=stale, env_working_dir=stale)

    deferral = DeferralReport()
    result = install._repair_systemd_unit_working_dir(
        install_path=install_path,
        deferral_report=deferral,
    )

    # Return value carries (old, new).
    assert result == (stale, correct)
    # Unit was rewritten with the correct path.
    rewritten = unit_path.read_text(encoding="utf-8")
    assert f"WorkingDirectory={correct}\n" in rewritten
    assert f"Environment=VCT_STACK_WORKING_DIR={correct}\n" in rewritten
    assert stale not in rewritten
    # Backup file landed alongside the unit.
    backups = list(unit_path.parent.glob(f"{unit_path.name}.bak-*"))
    assert len(backups) == 1
    # Backup contains the OLD content (stale path).
    backup_text = backups[0].read_text(encoding="utf-8")
    assert f"WorkingDirectory={stale}\n" in backup_text
    # Deferral entry added with the right metadata.
    entries = deferral.entries
    assert len(entries) == 1
    entry = entries[0]
    assert entry.condition_id == "boot_service_path_repaired"
    assert entry.severity == "info"
    assert stale in entry.detected
    assert correct in entry.detected
    assert "systemctl --user daemon-reload" in entry.command_to_apply
    assert "systemctl --user restart" in entry.command_to_apply


def test_repair_preserves_user_customisations(fake_home: Path, force_linux,
                                              tmp_path: Path):
    """A user who hand-edited the unit (e.g. to add an extra `After=`
    line) must keep those edits — the repair is in-place substitution,
    not a from-template re-render."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    custom = "After=multi-user.target\nRequires=network-online.target\n"
    unit_path = _write_unit(
        fake_home,
        working_dir="/stale",
        env_working_dir="/stale",
        extra_lines=custom,
    )

    install._repair_systemd_unit_working_dir(install_path=install_path)
    rewritten = unit_path.read_text(encoding="utf-8")
    # Custom lines preserved verbatim.
    assert "After=multi-user.target" in rewritten
    assert "Requires=network-online.target" in rewritten


def test_repair_skipped_when_no_working_dir_line(fake_home: Path, force_linux,
                                                  tmp_path: Path):
    """Defensive: a unit without a WorkingDirectory= line at all is
    treated as "not ours" — leave it alone, return None, no deferral."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()
    unit_dir = fake_home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_path = unit_dir / install._BOOT_SERVICE_UNIT_NAME
    unit_path.write_text(
        "[Unit]\nDescription=foo\n[Service]\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    mtime_before = unit_path.stat().st_mtime_ns

    deferral = DeferralReport()
    result = install._repair_systemd_unit_working_dir(
        install_path=install_path,
        deferral_report=deferral,
    )
    assert result is None
    assert unit_path.stat().st_mtime_ns == mtime_before
    assert deferral.entries == []


def test_repair_handles_correct_working_dir_param(fake_home: Path, force_linux,
                                                   tmp_path: Path):
    """When the caller passes `correct_working_dir` explicitly, the
    helper must use that and skip its own resolution path."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    # Note: NO claude_mcp_servers/ subdir — would cause auto-resolution
    # to fall through to None. The explicit param keeps it unambiguous.
    explicit = tmp_path / "explicit-compose-dir"
    explicit.mkdir()
    unit_path = _write_unit(fake_home, working_dir="/stale",
                            env_working_dir="/stale")

    result = install._repair_systemd_unit_working_dir(
        install_path=install_path,
        correct_working_dir=explicit,
    )
    assert result == ("/stale", str(explicit))
    assert f"WorkingDirectory={explicit}" in unit_path.read_text(encoding="utf-8")


def test_repair_returns_none_when_target_unresolvable(fake_home: Path,
                                                      force_linux, tmp_path: Path):
    """When neither install_path subdirs nor an explicit
    `correct_working_dir` resolves, the helper bails."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    # No subdirs, no explicit param.
    unit_path = _write_unit(fake_home, working_dir="/stale")
    mtime_before = unit_path.stat().st_mtime_ns

    result = install._repair_systemd_unit_working_dir(install_path=install_path)
    assert result is None
    # Unit untouched (no rewrite, no backup).
    assert unit_path.stat().st_mtime_ns == mtime_before
    backups = list(unit_path.parent.glob(f"{unit_path.name}.bak-*"))
    assert backups == []


def test_repair_soft_fail_on_unreadable_unit(fake_home: Path, force_linux,
                                              tmp_path: Path, monkeypatch):
    """A unit that can't be read (permission error) must not raise —
    log + return None."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()
    unit_path = _write_unit(fake_home, working_dir="/stale")

    real_read = Path.read_text

    def boom(self, *args, **kwargs):
        if self == unit_path:
            raise OSError("simulated permission denied")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    # Must not raise.
    result = install._repair_systemd_unit_working_dir(install_path=install_path)
    assert result is None


def test_repair_no_deferral_when_report_is_none(fake_home: Path, force_linux,
                                                 tmp_path: Path):
    """Caller may pass deferral_report=None (the default). Repair still
    happens; just no deferral entry is appended."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    unit_path = _write_unit(fake_home, working_dir="/stale",
                            env_working_dir="/stale")

    result = install._repair_systemd_unit_working_dir(install_path=install_path)
    assert result is not None
    # Unit was still repaired.
    assert "/stale" not in unit_path.read_text(encoding="utf-8")


def test_repair_does_not_inject_env_line_if_absent(fake_home: Path, force_linux,
                                                    tmp_path: Path):
    """If the user's unit has WorkingDirectory= but NOT the
    Environment=VCT_STACK_WORKING_DIR= line (e.g. an old hand-rolled
    unit predating that template), the substitution must not invent the
    line — only existing matches get rewritten."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    # env_working_dir=None → no Environment= line in the unit.
    unit_path = _write_unit(fake_home, working_dir="/stale", env_working_dir=None)

    install._repair_systemd_unit_working_dir(install_path=install_path)
    rewritten = unit_path.read_text(encoding="utf-8")
    # WorkingDirectory= was rewritten.
    assert f"WorkingDirectory={cms.resolve()}\n" in rewritten
    # No Environment= line was synthesised.
    assert "Environment=VCT_STACK_WORKING_DIR=" not in rewritten


# ---------------------------------------------------------------------------
# Integration: dispatcher passes deferral through
# ---------------------------------------------------------------------------


def test_dispatcher_invokes_repair_with_deferral(fake_home: Path, force_linux,
                                                  tmp_path: Path, monkeypatch):
    """`_materialize_boot_service` must invoke `_repair_systemd_unit_working_dir`
    with the deferral_report it was given, BEFORE the renderer runs."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    monkeypatch.setattr(install, "PROJECT_ROOT", install_path)
    # Stub out the OS-specific renderer so the test doesn't actually
    # write systemd files / call systemctl.
    monkeypatch.setattr(install, "_materialize_boot_service_linux",
                        lambda *a, **kw: None)

    deferral = DeferralReport()
    seen = {}

    real_repair = install._repair_systemd_unit_working_dir

    def spy(*, install_path, correct_working_dir=None, deferral_report=None):
        seen["called"] = True
        seen["install_path"] = install_path
        seen["correct_working_dir"] = correct_working_dir
        seen["deferral_report"] = deferral_report
        return real_repair(install_path=install_path,
                           correct_working_dir=correct_working_dir,
                           deferral_report=deferral_report)

    monkeypatch.setattr(install, "_repair_systemd_unit_working_dir", spy)

    import argparse as _argparse
    args = _argparse.Namespace(no_containers=False, compose_working_dir=None)
    install._materialize_boot_service(install_path, None, args,
                                      deferral_report=deferral)

    assert seen.get("called") is True
    assert seen.get("install_path") == install_path
    assert seen.get("correct_working_dir") == cms.resolve()
    assert seen.get("deferral_report") is deferral
