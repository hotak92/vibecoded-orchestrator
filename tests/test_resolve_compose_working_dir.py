# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pure-function tests for `_resolve_compose_working_dir` and
`_persist_runtime_txt` (v0.2.10 Bug L2 / L3; v0.2.97 SE-2).

The resolution helper is the cross-OS pivot for boot-service
materialization. Its whole matrix since v0.2.97:

    1. CLI override (--compose-working-dir) — a missing dir is None
    2. <install>/infrastructure/ — the installer's compose project
    3. None (give up, caller logs)

The legacy `<install>/claude_mcp_servers/` leg and the `claude-mcp`
ps-label leg are SUPERSEDED by infrastructure/ (every shipped layout has
it): containers the legacy home created are adopted by name through the
service_endpoints rows, never composed from there.

These tests do NOT touch the real filesystem outside tmp_path and do
NOT spawn subprocesses — they're hermetic and run on every OS.
"""
from __future__ import annotations

import sys
from pathlib import Path


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
    (install_path / "infrastructure").mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path, cli_override=str(override),
    )
    assert resolved == override.resolve()


def test_cli_override_missing_dir_returns_none(tmp_path: Path):
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "infrastructure").mkdir()
    resolved = install._resolve_compose_working_dir(
        install_path=install_path, cli_override=str(tmp_path / "does-not-exist"),
    )
    assert resolved is None


def test_infrastructure_is_the_working_dir(tmp_path: Path):
    install_path = tmp_path / "install"
    infra = install_path / "infrastructure"
    infra.mkdir(parents=True)
    resolved = install._resolve_compose_working_dir(install_path=install_path, cli_override=None)
    assert resolved == infra.resolve()


def test_the_legacy_home_is_never_the_working_dir(tmp_path: Path):
    """Superseded by infrastructure/: a checkout that has ONLY the legacy
    `claude_mcp_servers/` gets no working dir (the caller warns and asks for
    --compose-working-dir) rather than composing VCO's services there."""
    install_path = tmp_path / "install"
    (install_path / "claude_mcp_servers").mkdir(parents=True)
    assert install._resolve_compose_working_dir(install_path=install_path, cli_override=None) is None
    (install_path / "infrastructure").mkdir()
    assert install._resolve_compose_working_dir(
        install_path=install_path, cli_override=None) == (install_path / "infrastructure").resolve()


def test_no_candidate_returns_none(tmp_path: Path):
    install_path = tmp_path / "install"
    install_path.mkdir()
    assert install._resolve_compose_working_dir(install_path=install_path, cli_override=None) is None


def test_empty_string_override_treated_as_missing(tmp_path: Path):
    """Argparse may pass "" for an absent --compose-working-dir flag
    depending on caller wrapper. Treat as None."""
    install_path = tmp_path / "install"
    infra = install_path / "infrastructure"
    infra.mkdir(parents=True)
    resolved = install._resolve_compose_working_dir(install_path=install_path, cli_override="")
    assert resolved == infra.resolve()


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
