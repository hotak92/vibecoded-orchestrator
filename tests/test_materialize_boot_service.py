# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Mock-based tests for `_materialize_boot_service` and its OS-specific
helpers (v0.2.10 Bug L2 — cross-OS boot-service materialization).

Strategy:
  - Fake platform.system() to exercise each OS branch on a single host.
  - Fake subprocess.run to capture systemctl/launchctl/schtasks invocations
    without actually mutating the test host.
  - Use tmp_path everywhere instead of real ~/.config or ~/Library — we
    monkeypatch Path.home() to redirect.
  - All real templates live in templates/ already (Linux unit + new launchd
    plist + new Windows task XML) so the renderer can read them without
    further setup.

Soft-fail contract verification: missing binaries, missing templates,
write errors — none of these may raise to the caller. Each test asserts
the function returns without exception.
"""
from __future__ import annotations

import argparse
import os
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Safety net (PR-16 v0.2.11): bulletproof sandbox for user-home writes.
#
# Prior to PR-16, `_materialize_boot_service_linux`, `_materialize_boot_service_macos`,
# and PR-12's `_repair_systemd_unit_working_dir` called `Path.home()` directly.
# Tests that monkeypatched `_materialize_boot_service_linux` to raise (verifying
# soft-fail) still hit the repair step BEFORE the patched renderer, and the
# repair step's `Path.home()` returned the REAL user home — corrupting the
# developer's actual `~/.config/systemd/user/claude-mcp-containers.service`
# with the pytest `tmp_path` on every run.
#
# This autouse fixture sets `VCT_USER_HOME_OVERRIDE` to a per-test tmp dir
# for EVERY test in this file, regardless of whether the test author
# remembered to monkeypatch `Path.home()`. The override is consumed by
# `install._user_home_for_install()`, which is the single helper all four
# write callsites (systemd unit on Linux, launchd plist + log on macOS,
# PR-12's repair function) go through.
#
# Prevention, not recovery — a forgotten manual monkeypatch by a future
# test author can no longer corrupt user state.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sandbox_user_home(tmp_path, monkeypatch):
    """Redirect every boot-service write into a per-test tmp dir.

    Sets ``VCT_USER_HOME_OVERRIDE`` so `install._user_home_for_install()`
    returns ``tmp_path / "fake_home"`` instead of the real `Path.home()`.
    This catches any write that the test code itself didn't anticipate
    (notably `_repair_systemd_unit_working_dir`, which the dispatcher
    invokes BEFORE the OS-specific renderer).
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(fake_home))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ns(**overrides) -> argparse.Namespace:
    """Build an argparse.Namespace with all the fields the materializer
    reads via getattr (so we don't trip AttributeError on uninitialised
    flags from the real argparse setup)."""
    base = {
        "no_containers": False,
        "compose_working_dir": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _fake_run_log(calls: list):
    """Return a subprocess.run replacement that appends every call's argv
    to `calls` and returns a successful CompletedProcess. Used to verify
    the materializer fires the right OS-specific binaries."""
    def run(argv, *a, **kw):
        calls.append(list(argv) if isinstance(argv, (list, tuple)) else [argv])
        return types.SimpleNamespace(
            returncode=0, stdout="", stderr="",
        )
    return run


def _fake_which(present: set[str]):
    def which(cmd):
        return f"/usr/bin/{cmd}" if cmd in present else None
    return which


# ---------------------------------------------------------------------------
# Cross-cutting soft-fail tests
# ---------------------------------------------------------------------------


def test_no_containers_flag_skips_materialization(tmp_path, monkeypatch):
    """--no-containers users have no stack to autostart. The dispatcher
    must short-circuit BEFORE attempting any OS-specific work."""
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    monkeypatch.setattr(install.shutil, "which", _fake_which({"systemctl"}))
    install._materialize_boot_service(tmp_path, None, _ns(no_containers=True))
    assert calls == [], f"Expected no subprocess calls; got {calls}"


def test_disable_env_var_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("VCT_DISABLE_BOOT_SERVICE", "1")
    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    install._materialize_boot_service(tmp_path, None, _ns())
    assert calls == []


def test_no_compose_working_dir_resolved_skips_oses(tmp_path, monkeypatch):
    """When _resolve_compose_working_dir returns None, the dispatcher
    must log + skip — no OS-specific renderer runs."""
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    # Empty install dir → no claude_mcp_servers, no infrastructure → None
    empty_install = tmp_path / "empty"
    empty_install.mkdir()
    install._materialize_boot_service(empty_install, None, _ns())
    assert calls == []


def test_unsupported_os_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "PROJECT_ROOT", tmp_path)
    (tmp_path / "infrastructure").mkdir()
    monkeypatch.setattr(install.platform, "system", lambda: "FreeBSD")
    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    install._materialize_boot_service(tmp_path, None, _ns())
    # Dispatcher logged + returned; no OS-specific work.
    assert calls == []


# ---------------------------------------------------------------------------
# Linux branch
# ---------------------------------------------------------------------------


def test_linux_materializes_systemd_unit_when_systemctl_present(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    # Use the sandboxed home that the autouse `_sandbox_user_home` fixture set.
    # (PR-16 v0.2.11: writes go to VCT_USER_HOME_OVERRIDE, not Path.home().)
    fake_home = Path(os.environ["VCT_USER_HOME_OVERRIDE"])
    monkeypatch.setenv("USER", "testuser")
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()
    (install_path / "scripts").mkdir()
    (install_path / "scripts" / "launch-claude-mcp-stack.sh").write_text("#!/bin/bash\n")

    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    monkeypatch.setattr(
        install.shutil, "which",
        _fake_which({"systemctl", "loginctl"}),
    )

    install._materialize_boot_service(install_path, None, _ns())

    unit = fake_home / ".config" / "systemd" / "user" / "claude-mcp-containers.service"
    assert unit.is_file(), "systemd unit should be written"
    body = unit.read_text()
    # Substitutions landed.
    assert str(install_path / "claude_mcp_servers") in body
    assert "launch-claude-mcp-stack.sh" in body
    # systemctl invocations fired.
    cmd_strs = [" ".join(c) for c in calls]
    assert any("daemon-reload" in s for s in cmd_strs)
    assert any("enable" in s for s in cmd_strs)


def test_linux_skips_systemctl_when_missing(tmp_path, monkeypatch):
    """systemctl absent (container / WSL minimal): unit file still gets
    written, but no daemon-reload / enable is attempted."""
    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    # Sandboxed home via PR-16 autouse fixture.
    fake_home = Path(os.environ["VCT_USER_HOME_OVERRIDE"])  # noqa: F841 (kept for documentation)
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()

    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    monkeypatch.setattr(install.shutil, "which", _fake_which(set()))

    # Must not raise.
    install._materialize_boot_service(install_path, None, _ns())
    # No systemctl invocation because it isn't on PATH.
    assert calls == []


def test_linux_idempotent_rewrite(tmp_path, monkeypatch):
    """Re-running with unchanged substitutions must not bump the unit
    file's mtime."""
    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    # Sandboxed home via PR-16 autouse fixture.
    fake_home = Path(os.environ["VCT_USER_HOME_OVERRIDE"])
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()

    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    monkeypatch.setattr(install.shutil, "which", _fake_which({"systemctl"}))

    install._materialize_boot_service(install_path, None, _ns())
    unit = fake_home / ".config" / "systemd" / "user" / "claude-mcp-containers.service"
    first_mtime = unit.stat().st_mtime_ns
    import time as _t
    _t.sleep(0.01)
    install._materialize_boot_service(install_path, None, _ns())
    # Same content → no rewrite.
    assert unit.stat().st_mtime_ns == first_mtime


# ---------------------------------------------------------------------------
# macOS branch
# ---------------------------------------------------------------------------


def test_macos_materializes_launchagent_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    # Sandboxed home via PR-16 autouse fixture.
    fake_home = Path(os.environ["VCT_USER_HOME_OVERRIDE"])
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()

    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    monkeypatch.setattr(install.shutil, "which", _fake_which({"launchctl"}))

    install._materialize_boot_service(install_path, None, _ns())

    plist = (fake_home / "Library" / "LaunchAgents"
             / "com.vibecodedtools.claude-mcp-containers.plist")
    assert plist.is_file()
    body = plist.read_text()
    assert "<key>Label</key>" in body
    assert "com.vibecodedtools.claude-mcp-containers" in body
    assert "RunAtLoad" in body
    # launchctl bootstrap was attempted.
    assert any("launchctl" in " ".join(c) for c in calls)


def test_macos_skips_launchctl_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    # Sandboxed home via PR-16 autouse fixture.
    fake_home = Path(os.environ["VCT_USER_HOME_OVERRIDE"])
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()

    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    monkeypatch.setattr(install.shutil, "which", _fake_which(set()))

    # Must not raise; plist is still written.
    install._materialize_boot_service(install_path, None, _ns())
    plist = (fake_home / "Library" / "LaunchAgents"
             / "com.vibecodedtools.claude-mcp-containers.plist")
    assert plist.is_file()
    assert calls == []


# ---------------------------------------------------------------------------
# Windows branch
# ---------------------------------------------------------------------------


def test_windows_materializes_task_xml(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    monkeypatch.setenv("USERDOMAIN", "TESTDOM")
    monkeypatch.setenv("USERNAME", "tester")
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()

    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    monkeypatch.setattr(install.shutil, "which", _fake_which({"schtasks"}))

    install._materialize_boot_service(install_path, None, _ns())

    task_xml = install_path / "state" / "installed_boot_task.xml"
    assert task_xml.is_file()
    body = task_xml.read_text()
    assert "<LogonTrigger>" in body
    assert "TESTDOM\\tester" in body
    # schtasks /Create /XML invocation fired.
    cmd_strs = [" ".join(c) for c in calls]
    assert any("schtasks" in s and "/Create" in s and "/XML" in s for s in cmd_strs)


def test_windows_skips_schtasks_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()

    calls = []
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log(calls))
    monkeypatch.setattr(install.shutil, "which", _fake_which(set()))

    install._materialize_boot_service(install_path, None, _ns())
    # XML is still materialized as an audit artefact.
    assert (install_path / "state" / "installed_boot_task.xml").is_file()
    assert calls == []


# ---------------------------------------------------------------------------
# _probe_compose_working_dir_via_ps — soft-fail
# ---------------------------------------------------------------------------


def test_probe_ps_returns_none_when_runtime_missing(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _: None)
    assert install._probe_compose_working_dir_via_ps("podman") is None


def test_probe_ps_returns_none_on_empty_string(monkeypatch):
    assert install._probe_compose_working_dir_via_ps("") is None


def test_probe_ps_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda _: "/usr/bin/podman")
    def run(*a, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
    monkeypatch.setattr(install.subprocess, "run", run)
    assert install._probe_compose_working_dir_via_ps("podman") is None


def test_probe_ps_parses_label(monkeypatch, tmp_path):
    monkeypatch.setattr(install.shutil, "which", lambda _: "/usr/bin/podman")
    label = "/home/u/code/orch/claude_mcp_servers"
    def run(*a, **kw):
        return types.SimpleNamespace(returncode=0, stdout=label + "\n", stderr="")
    monkeypatch.setattr(install.subprocess, "run", run)
    assert install._probe_compose_working_dir_via_ps("podman") == label


def test_probe_ps_handles_multiline_output(monkeypatch):
    """`podman ps` may emit one line per container — take the first
    non-empty value."""
    monkeypatch.setattr(install.shutil, "which", lambda _: "/usr/bin/podman")
    def run(*a, **kw):
        return types.SimpleNamespace(
            returncode=0,
            stdout="\n/tmp/first\n/tmp/second\n",
            stderr="",
        )
    monkeypatch.setattr(install.subprocess, "run", run)
    assert install._probe_compose_working_dir_via_ps("podman") == "/tmp/first"


# ---------------------------------------------------------------------------
# Exception-safety: even an internal raise must not propagate.
# ---------------------------------------------------------------------------


def test_internal_exception_does_not_propagate(tmp_path, monkeypatch):
    """If the OS-specific renderer raises (e.g. a Path operation explodes
    on a malformed home), the dispatcher must catch + log + return.

    PR-16 v0.2.11: this test historically caused a real-user-state leak.
    Before PR-16, the dispatcher's pre-renderer call to
    `_repair_systemd_unit_working_dir` wrote the pytest `tmp_path` into
    the developer's actual `~/.config/systemd/user/claude-mcp-containers.service`
    (because the repair function called `Path.home()` directly).
    The autouse `_sandbox_user_home` fixture now redirects every
    user-home write into `tmp_path / "fake_home"`, so this test is safe
    to re-run on any developer's box.
    """
    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    (tmp_path / "claude_mcp_servers").mkdir()
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")

    def boom(*a, **kw):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(install, "_materialize_boot_service_linux", boom)

    # Must not raise.
    install._materialize_boot_service(tmp_path, None, _ns())


# ---------------------------------------------------------------------------
# PR-16 v0.2.11: prevention tests for the user-home sandbox.
#
# Verifies that:
#   1. `_user_home_for_install()` honors VCT_USER_HOME_OVERRIDE when set.
#   2. `_user_home_for_install()` falls back to `Path.home()` when unset.
#   3. The autouse `_sandbox_user_home` fixture actually keeps writes
#      out of the real user systemd dir (regression guard).
# ---------------------------------------------------------------------------


def test_user_home_for_install_honors_env_override(tmp_path, monkeypatch):
    """Setting VCT_USER_HOME_OVERRIDE must redirect the resolved home."""
    override = tmp_path / "override_home"
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(override))
    assert install._user_home_for_install() == override


def test_user_home_for_install_falls_back_to_path_home(tmp_path, monkeypatch):
    """With VCT_USER_HOME_OVERRIDE unset, must return Path.home()."""
    monkeypatch.delenv("VCT_USER_HOME_OVERRIDE", raising=False)
    fake = tmp_path / "real_home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    assert install._user_home_for_install() == fake


def test_user_home_for_install_strips_whitespace(tmp_path, monkeypatch):
    """A whitespace-only override is treated as unset (falls back)."""
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", "   ")
    fake = tmp_path / "fallback"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    assert install._user_home_for_install() == fake


def test_dispatcher_never_writes_to_real_user_systemd_dir(tmp_path, monkeypatch):
    """Regression guard for the 2026-05-16 corruption incident.

    Even when the test ITSELF does not monkeypatch Path.home, the autouse
    `_sandbox_user_home` fixture must keep the dispatcher (including its
    pre-renderer `_repair_systemd_unit_working_dir` call) out of the real
    user systemd dir.

    The assertion: after running the full Linux dispatch path with a
    `tmp_path` install root, the real `Path.home() / .config/systemd/user/
    claude-mcp-containers.service` is either unchanged (best case) or
    untouched (the fixture redirected the write into fake_home).
    """
    real_unit = Path.home() / ".config" / "systemd" / "user" / "claude-mcp-containers.service"
    mtime_before = real_unit.stat().st_mtime if real_unit.exists() else None

    monkeypatch.setattr(install, "PROJECT_ROOT", Path(__file__).resolve().parent.parent)
    install_path = tmp_path / "install"
    install_path.mkdir()
    (install_path / "claude_mcp_servers").mkdir()
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.subprocess, "run", _fake_run_log([]))
    monkeypatch.setattr(install.shutil, "which", _fake_which({"systemctl", "loginctl"}))

    install._materialize_boot_service(install_path, None, _ns())

    mtime_after = real_unit.stat().st_mtime if real_unit.exists() else None
    assert mtime_before == mtime_after, (
        "Real user systemd unit was modified by the dispatcher! "
        "The VCT_USER_HOME_OVERRIDE sandbox failed — see PR-16 in install.py."
    )

    # Verify the WRITE actually happened in the sandboxed home:
    fake_home = Path(os.environ["VCT_USER_HOME_OVERRIDE"])
    sandboxed_unit = fake_home / ".config" / "systemd" / "user" / "claude-mcp-containers.service"
    # The unit may or may not exist depending on whether the dispatcher
    # made it to the renderer; what matters is the REAL one was untouched.
    if sandboxed_unit.exists():
        body = sandboxed_unit.read_text(encoding="utf-8")
        assert str(install_path) in body, (
            "Sandboxed unit doesn't reference the test's install_path — "
            "the write went somewhere else?"
        )
