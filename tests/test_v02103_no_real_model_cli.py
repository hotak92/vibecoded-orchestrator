"""W-MODEL-CLI (v0.2.103): the suite can never reach a REAL model CLI.

Motivation: on 2026-10-15 a test removed only its own fake ``claude`` and kept
the inherited PATH, so the shipped code found the maintainer's real CLI and the
suite launched two real ``claude -p`` Sonnet runs (paid calls, a real headless
session). ``tests/conftest.py`` now puts a refusing stub directory first on
PATH for every test. These tests pin the three ways a test reaches a binary:
the default env, an env built by hand from ``os.environ``, and its own fake.

None of them ever executes a real model CLI: the red-proof leg uses
``shutil.which`` only.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env

REPO = Path(__file__).resolve().parent.parent


def _assert_stub_answered(proc: subprocess.CompletedProcess, hits) -> None:
    assert proc.returncode == hits.exit_code, proc
    assert hits.message in proc.stderr, proc.stderr
    drained = hits.drain()
    assert len(drained) == 1 and drained[0].endswith("claude --version"), drained


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script; .cmd covered by which()")
def test_default_env_claude_hits_the_stub(model_cli_stub_hits):
    """`claude --version` with the inherited environment reaches the stub."""
    assert shutil.which("claude") == str(model_cli_stub_hits.stub_dir / "claude")
    proc = subprocess.run(["claude", "--version"], capture_output=True,
                          text=True, timeout=30)
    _assert_stub_answered(proc, model_cli_stub_hits)


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script; .cmd covered by which()")
def test_env_built_from_os_environ_still_hits_the_stub(model_cli_stub_hits):
    """A child env copied from ``os.environ`` (the incident's shape: a test
    that strips only ITS OWN fake dir and keeps the rest) still finds the stub."""
    own_fake_dir = "/nonexistent/own-fake-bin"
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        p for p in (own_fake_dir + os.pathsep + env["PATH"]).split(os.pathsep)
        if p != own_fake_dir)
    assert shutil.which("claude", path=env["PATH"]) == str(
        model_cli_stub_hits.stub_dir / "claude")
    proc = subprocess.run(["claude", "--version"], capture_output=True,
                          text=True, timeout=30, env=env)
    _assert_stub_answered(proc, model_cli_stub_hits)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell fake")
def test_a_test_owned_fake_on_path_wins(tmp_path, monkeypatch, model_cli_stub_hits):
    """The existing fake-claude convention (prepend the fake's dir) is not
    shadowed by the stub, and the stub records nothing."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text("#!/bin/sh\necho fake-claude 9.9.9\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    proc = subprocess.run(["claude", "--version"], capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0 and proc.stdout.strip() == "fake-claude 9.9.9"
    assert model_cli_stub_hits.drain() == []


def test_windows_stub_present_beside_the_posix_one(model_cli_stub_hits):
    """``claude.cmd`` exists so ``shutil.which`` / ``Get-Command`` on Windows
    also resolve the stub first."""
    cmd = model_cli_stub_hits.stub_dir / "claude.cmd"
    assert cmd.is_file()
    text = cmd.read_text(encoding="utf-8")
    assert f"exit /b {model_cli_stub_hits.exit_code}" in text
    assert model_cli_stub_hits.message in text


def test_stub_is_reestablished_after_a_test_dropped_it(model_cli_stub_hits):
    """A test that overwrote PATH without the stub (and did not restore) must
    not un-protect the next test: the per-test setup puts it back first."""
    stub = model_cli_stub_hits.stub_dir
    saved = os.environ["PATH"]
    try:
        os.environ["PATH"] = model_cli_stub_hits.path_without(saved, stub)
        assert str(stub) not in os.environ["PATH"].split(os.pathsep)
        model_cli_stub_hits.ensure_on_path()
        assert os.environ["PATH"].split(os.pathsep)[0] == str(stub)
    finally:
        os.environ["PATH"] = saved


def test_reestablish_never_shadows_a_fake_already_ahead(tmp_path, model_cli_stub_hits):
    """A fake placed AHEAD of the stub (module-scoped setup) keeps winning."""
    saved = os.environ["PATH"]
    try:
        os.environ["PATH"] = f"{tmp_path}{os.pathsep}{saved}"
        model_cli_stub_hits.ensure_on_path()
        assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)
    finally:
        os.environ["PATH"] = saved


@pytest.mark.allow_real_model_cli
def test_opt_in_marker_removes_the_stub_dir(model_cli_stub_hits):
    """The opt-in removes the stub from PATH for that test only. Asserted via
    PATH contents — this test never runs any `claude`."""
    assert str(model_cli_stub_hits.stub_dir) not in os.environ["PATH"].split(os.pathsep)


def test_opt_in_did_not_leak_into_the_next_test(model_cli_stub_hits):
    assert str(model_cli_stub_hits.stub_dir) in os.environ["PATH"].split(os.pathsep)


@pytest.mark.skipif(os.name == "nt", reason="POSIX stub script")
def test_an_undrained_hit_reds_the_test(tmp_path):
    """The per-test check reds a test whose shipped caller swallowed the
    stub's refusal. Run in a child pytest (with this conftest loaded as a
    plugin) so the red is observed, not suffered."""
    child = tmp_path / "test_child_hits_stub.py"
    child.write_text(
        "import subprocess\n"
        "def test_swallowed():\n"
        "    subprocess.run(['claude', '-p', 'hi'], capture_output=True)\n",
        encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "tests.conftest", "--rootdir", str(tmp_path), str(child)],
        capture_output=True, text=True, timeout=300, cwd=REPO, env=child_env())
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "reached the W-MODEL-CLI" in out and "claude -p hi" in out, out


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell fake")
def test_shared_fake_claude_cli_answers_version_only(fake_claude_cli, model_cli_stub_hits):
    """The shared `fake_claude_cli` fixture (for tests whose code path only
    PROBES the CLI) answers `--version`, refuses a model turn loudly, records
    both calls, and keeps the stub untouched."""
    ok = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                        timeout=60)
    assert ok.returncode == 0 and "Claude Code" in ok.stdout, ok
    turn = subprocess.run(["claude", "-p", "hi"], capture_output=True, text=True,
                          timeout=60)
    assert turn.returncode == 1 and "only --version is faked" in turn.stderr, turn
    assert fake_claude_cli.calls() == [["--version"], ["-p", "hi"]]
    assert model_cli_stub_hits.drain() == []
