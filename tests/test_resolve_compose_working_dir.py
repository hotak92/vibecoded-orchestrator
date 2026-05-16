# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pure-function tests for `_resolve_compose_working_dir` and
`_persist_runtime_txt` (v0.2.10 Bug L2 / L3, updated PR-12 v0.2.11 Bug C).

The resolution helper is the cross-OS pivot for boot-service
materialization: it has to pick the right compose-project directory
without spawning a container runtime. We test the priority matrix
(NEW priority order, post-PR-12):

    1. CLI override (--compose-working-dir)
    2. <install>/claude_mcp_servers/
    3. <install>/infrastructure/
    4. ps-label probe result (caller-supplied so this stays pure) —
       last-resort fallback only
    5. None (give up, caller logs)

The PR-12 inversion (ps_label demoted from priority 2 to priority 4)
prevents stale `com.docker.compose.project.working_dir` labels from
prior installs from pinning the boot-service WorkingDirectory to an
obsolete path across upgrades.

These tests do NOT touch the real filesystem outside tmp_path and do
NOT spawn subprocesses — they're hermetic and run on every OS.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# _resolve_compose_working_dir — priority matrix
# ---------------------------------------------------------------------------


def test_cli_override_wins_when_dir_exists(tmp_path: Path):
    override = tmp_path / "explicit-compose-dir"
    override.mkdir()
    install_path = tmp_path / "install"
    install_path.mkdir()
    # Even if other candidates exist, CLI override beats them all.
    (install_path / "claude_mcp_servers").mkdir()
    (install_path / "infrastructure").mkdir()

    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=str(override),
        ps_label_value=str(install_path / "claude_mcp_servers"),
    )
    assert resolved == override.resolve()


def test_cli_override_missing_dir_returns_none(tmp_path: Path):
    # Override points at a non-existent dir → resolution returns None
    # (per design: explicit user error worth surfacing — not a silent
    # fall-through to a fallback that might be wrong).
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=str(tmp_path / "does-not-exist"),
        ps_label_value=None,
    )
    assert resolved is None


def test_install_subdir_wins_over_ps_label(tmp_path: Path):
    """PR-12 Bug C: when both `<install>/claude_mcp_servers/` AND a
    ps_label_value exist, the install subdir wins. This is the priority
    inversion that prevents stale containers from a prior install path
    from pinning the new boot-service WorkingDirectory.

    Pre-PR-12 this assertion was the OPPOSITE (ps_label won)."""
    label_dir = tmp_path / "label-compose-dir"
    label_dir.mkdir()
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()

    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=None,
        ps_label_value=str(label_dir),
    )
    # NEW priority order — install subdir wins over ps_label.
    assert resolved == cms.resolve()


def test_ps_label_used_only_when_no_install_subdir(tmp_path: Path):
    """PR-12 Bug C: ps_label is now the LAST-RESORT fallback (priority
    4). It only takes effect when neither
    `<install>/claude_mcp_servers/` nor `<install>/infrastructure/`
    exists locally — the rare edge case where compose.yaml ships in a
    sibling repo entirely outside install_path."""
    label_dir = tmp_path / "label-compose-dir"
    label_dir.mkdir()
    install_path = tmp_path / "install"
    install_path.mkdir()
    # Note: no claude_mcp_servers/ or infrastructure/ subdirs created.

    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=None,
        ps_label_value=str(label_dir),
    )
    assert resolved == label_dir.resolve()


def test_ps_label_missing_dir_falls_through(tmp_path: Path):
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=None,
        ps_label_value=str(tmp_path / "label-does-not-exist"),
    )
    # ps label dir doesn't exist → install/claude_mcp_servers wins
    # (also wins on the new priority order even if the label dir DID
    # exist — covered by test_install_subdir_wins_over_ps_label).
    assert resolved == cms.resolve()


def test_ps_label_skipped_when_install_subdir_present(tmp_path: Path):
    """PR-12 Bug C regression guard: even when ps_label points at a
    perfectly valid existing dir that is DIFFERENT from the install
    subdirs, the install subdir still wins. This is the canonical
    "stale prior-install container" scenario."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    # A stale ps-label value pointing at an old install location that
    # still exists on disk (common: user kept the old install around).
    stale_old_install = tmp_path / "old-install" / "claude_mcp_servers"
    stale_old_install.mkdir(parents=True)
    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=None,
        ps_label_value=str(stale_old_install),
    )
    # Must resolve to the NEW install location, not the stale one.
    assert resolved == cms.resolve()
    assert resolved != stale_old_install.resolve()


def test_install_claude_mcp_servers_fallback(tmp_path: Path):
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=None,
        ps_label_value=None,
    )
    assert resolved == cms.resolve()


def test_install_infrastructure_fallback(tmp_path: Path):
    """When claude_mcp_servers/ doesn't exist, fall through to
    infrastructure/ — the VCO-native layout."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    infra = install_path / "infrastructure"
    infra.mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=None,
        ps_label_value=None,
    )
    assert resolved == infra.resolve()


def test_claude_mcp_servers_preferred_over_infrastructure(tmp_path: Path):
    """When both exist, claude_mcp_servers/ wins (priority 3 < priority 4
    in the resolver's spec)."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    (install_path / "infrastructure").mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=None,
        ps_label_value=None,
    )
    assert resolved == cms.resolve()


def test_no_candidate_returns_none(tmp_path: Path):
    """Empty install dir, no override, no ps label → None."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override=None,
        ps_label_value=None,
    )
    assert resolved is None


def test_empty_string_override_treated_as_missing(tmp_path: Path):
    """Argparse may pass "" for an absent --compose-working-dir flag
    depending on caller wrapper. Treat as None."""
    install_path = tmp_path / "install"
    install_path.mkdir()
    cms = install_path / "claude_mcp_servers"
    cms.mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path,
        cli_override="",
        ps_label_value=None,
    )
    # Empty string falsey → skip override → fall through to claude_mcp_servers
    assert resolved == cms.resolve()


# ---------------------------------------------------------------------------
# _persist_runtime_txt — idempotent runtime.txt writes (L3)
# ---------------------------------------------------------------------------


def test_persist_runtime_txt_writes_podman(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    install._persist_runtime_txt("podman 4.9.0")
    runtime_file = tmp_path / "state" / "install" / "runtime.txt"
    assert runtime_file.read_text() == "podman\n"


def test_persist_runtime_txt_writes_docker(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    install._persist_runtime_txt("docker")
    assert (tmp_path / "state" / "install" / "runtime.txt").read_text() == "docker\n"


def test_persist_runtime_txt_lowercases(monkeypatch, tmp_path: Path):
    """Some package builds report "Podman" / "Docker" with title case."""
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    install._persist_runtime_txt("Podman 5.0.1")
    assert (tmp_path / "state" / "install" / "runtime.txt").read_text() == "podman\n"


def test_persist_runtime_txt_rejects_unknown(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    install._persist_runtime_txt("nerdctl 1.7")
    # Unknown runtime token — file should NOT be created.
    assert not (tmp_path / "state" / "install" / "runtime.txt").exists()


def test_persist_runtime_txt_idempotent_no_rewrite(monkeypatch, tmp_path: Path):
    """When the file already contains the same token, don't rewrite (mtime
    must not change). Important for downstream watchers that key off
    runtime.txt mtime."""
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    install._persist_runtime_txt("podman")
    runtime_file = tmp_path / "state" / "install" / "runtime.txt"
    mtime_before = runtime_file.stat().st_mtime_ns
    # Force a perceptible time gap then call again.
    import time as _t
    _t.sleep(0.01)
    install._persist_runtime_txt("podman")
    assert runtime_file.stat().st_mtime_ns == mtime_before


def test_persist_runtime_txt_rewrites_on_change(monkeypatch, tmp_path: Path):
    """Switching from podman → docker (or vice versa) MUST update the
    file. Common case: user runs `apt install docker` between two
    install runs."""
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    install._persist_runtime_txt("podman")
    install._persist_runtime_txt("docker")
    runtime_file = tmp_path / "state" / "install" / "runtime.txt"
    assert runtime_file.read_text() == "docker\n"


def test_persist_runtime_txt_none_is_noop(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    install._persist_runtime_txt(None)
    assert not (tmp_path / "state" / "install" / "runtime.txt").exists()


def test_persist_runtime_txt_empty_string_is_noop(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    install._persist_runtime_txt("")
    assert not (tmp_path / "state" / "install" / "runtime.txt").exists()
