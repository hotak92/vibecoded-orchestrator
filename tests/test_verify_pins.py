# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for ``vco verify-pins`` (Phase 0.C acceptance).

Stubbed Phase 0.A APIs (post-merge integration checklist for the
reviewer):

* ``vco_lib.bundled_versions.load_bundled_versions() -> Mapping[str, NpmPin]``
  — each value must expose ``.package`` (str) and ``.version`` (str)
  attributes (or be a dict with those keys; our accessor handles both).
* ``install._install_pinned_npm(package_key: str) -> InstallResult``
  — return value must expose either ``.ok`` (bool) and ``.message`` (str)
  attributes, or be a dict with those keys.

Coverage:
* All-match → exit 0, JSON envelope sane.
* Single drift → exit 1, drift table includes the offending row.
* Missing package (npm list returns nothing) → exit 1, status=missing.
* npm not on PATH → exit 2, no further work attempted.
* ``--json`` schema matches the documented envelope.
* ``--fix`` invokes the install helper for each drift row.
* ``--fix`` aborts on the first install failure (does NOT silently skip).
* ``--fix`` re-runs verification afterwards (idempotency loop).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.cli import verify  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pin(package: str, version: str) -> dict[str, str]:
    """Phase 0.A pin shape — we use a dict because the real dataclass
    isn't shipped yet; the accessor handles both."""
    return {"package": package, "version": version, "sha256": "0" * 64}


@pytest.fixture(autouse=True)
def _reset_which_cache():
    """Each test gets a clean cached-which slate."""
    verify._reset_which_cache()
    yield
    verify._reset_which_cache()


@pytest.fixture
def two_pins() -> dict[str, dict[str, str]]:
    return {
        "mermaid_mcp": _make_pin("claude-mermaid", "1.4.2"),
        "excalidraw_mcp": _make_pin(
            "@sanjibdevnathlabs/mcp-excalidraw-local", "0.3.1"
        ),
    }


def _stub_manifest(monkeypatch, pins: Mapping[str, Any]) -> None:
    monkeypatch.setattr(verify, "_load_bundled_versions", lambda: pins)


def _stub_npm_path(monkeypatch, npm_path: str | None) -> None:
    monkeypatch.setattr(verify, "_which", lambda tool: npm_path if tool == "npm" else None)


def _stub_npm_view(monkeypatch, installed: Mapping[str, str | None]) -> None:
    """Patch ``_npm_view_version`` to return ``installed[package]``.

    Missing keys → ``None`` (i.e. package not installed).
    """
    def _fake(package: str, *, npm_path: str) -> str | None:
        return installed.get(package)
    monkeypatch.setattr(verify, "_npm_view_version", _fake)


def _args(json_mode: bool = False, fix: bool = False) -> argparse.Namespace:
    return argparse.Namespace(json=json_mode, fix=fix)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_match_exits_zero(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    _stub_npm_view(
        monkeypatch,
        {
            "claude-mermaid": "1.4.2",
            "@sanjibdevnathlabs/mcp-excalidraw-local": "0.3.1",
        },
    )
    code = verify.cmd_verify_pins(_args())
    assert code == verify.EXIT_OK
    out = capsys.readouterr().out
    assert "OK" in out
    # Human table header tokens stable for grep-friendly downstream tooling.
    assert "package" in out
    assert "pinned" in out
    assert "installed" in out
    assert "status" in out


def test_single_drift_exits_one(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    _stub_npm_view(
        monkeypatch,
        {
            "claude-mermaid": "1.4.1",  # drift!
            "@sanjibdevnathlabs/mcp-excalidraw-local": "0.3.1",
        },
    )
    code = verify.cmd_verify_pins(_args())
    assert code == verify.EXIT_DRIFT
    cap = capsys.readouterr()
    assert "DRIFT" in cap.out
    assert "claude-mermaid" in cap.out
    assert "1.4.1" in cap.out
    assert "1.4.2" in cap.out
    assert "--fix" in cap.err


def test_missing_package_classified_as_missing(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    _stub_npm_view(
        monkeypatch,
        {"claude-mermaid": "1.4.2"},  # the other one is None → missing
    )
    code = verify.cmd_verify_pins(_args())
    assert code == verify.EXIT_DRIFT
    out = capsys.readouterr().out
    assert "missing" in out


def test_npm_not_available_exits_two(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, None)

    # If npm is missing we should NEVER call npm-view; assert that.
    call_count = {"n": 0}

    def _should_not_run(*a, **k):
        call_count["n"] += 1
        raise AssertionError("npm-view called despite npm-not-available")

    monkeypatch.setattr(verify, "_npm_view_version", _should_not_run)

    code = verify.cmd_verify_pins(_args())
    assert code == verify.EXIT_TOOL_MISSING
    assert call_count["n"] == 0
    err = capsys.readouterr().err
    assert "npm not available" in err


def test_json_schema_all_match(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    _stub_npm_view(
        monkeypatch,
        {
            "claude-mermaid": "1.4.2",
            "@sanjibdevnathlabs/mcp-excalidraw-local": "0.3.1",
        },
    )
    code = verify.cmd_verify_pins(_args(json_mode=True))
    assert code == verify.EXIT_OK
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["command"] == "verify-pins"
    assert payload["exit_code"] == verify.EXIT_OK
    assert payload["overall"] == "ok"
    rows = payload["rows"]
    assert len(rows) == 2
    for row in rows:
        assert set(row.keys()) >= {"key", "package", "pinned", "installed", "status"}
        assert row["status"] == "match"


def test_json_schema_drift_includes_rows(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    _stub_npm_view(
        monkeypatch,
        {
            "claude-mermaid": "1.4.1",
            "@sanjibdevnathlabs/mcp-excalidraw-local": "0.3.1",
        },
    )
    code = verify.cmd_verify_pins(_args(json_mode=True))
    assert code == verify.EXIT_DRIFT
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["overall"] == "drift"
    statuses = {row["status"] for row in payload["rows"]}
    assert "drift" in statuses


def test_fix_calls_installer_per_drift(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    # First call: one row drifts; second call (the post-fix re-verify):
    # everything matches.
    call_state = {"phase": "pre-fix"}

    def _fake_view(package, *, npm_path):
        if call_state["phase"] == "pre-fix":
            mapping = {
                "claude-mermaid": "1.4.1",
                "@sanjibdevnathlabs/mcp-excalidraw-local": "0.3.1",
            }
        else:
            mapping = {
                "claude-mermaid": "1.4.2",
                "@sanjibdevnathlabs/mcp-excalidraw-local": "0.3.1",
            }
        return mapping.get(package)

    monkeypatch.setattr(verify, "_npm_view_version", _fake_view)

    installer_calls: list[str] = []

    def _fake_install(key: str):
        installer_calls.append(key)
        call_state["phase"] = "post-fix"
        return {"ok": True, "message": f"installed {key}"}

    monkeypatch.setattr(verify, "_install_pinned_npm", _fake_install)

    code = verify.cmd_verify_pins(_args(fix=True))
    # After --fix + idempotent re-verify, we expect OK.
    assert code == verify.EXIT_OK
    # Installer called once for the one drift row.
    assert installer_calls == ["mermaid_mcp"]


def test_fix_aborts_on_first_failure(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    _stub_npm_view(
        monkeypatch,
        {
            "claude-mermaid": "1.4.1",        # drift
            "@sanjibdevnathlabs/mcp-excalidraw-local": "0.2.0",  # drift
        },
    )

    installer_calls: list[str] = []

    def _fake_install(key: str):
        installer_calls.append(key)
        return {"ok": False, "message": "registry timeout"}

    monkeypatch.setattr(verify, "_install_pinned_npm", _fake_install)

    code = verify.cmd_verify_pins(_args(fix=True))
    assert code == verify.EXIT_USAGE
    # Aborted on first failure — only one install attempted.
    assert len(installer_calls) == 1
    err = capsys.readouterr().err
    assert "aborting on first failure" in err
    assert "registry timeout" in err


def test_fix_json_failure_envelope(monkeypatch, two_pins, capsys):
    _stub_manifest(monkeypatch, two_pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    _stub_npm_view(
        monkeypatch,
        {
            "claude-mermaid": "1.4.1",
            "@sanjibdevnathlabs/mcp-excalidraw-local": "0.3.1",
        },
    )

    def _fake_install(key: str):
        return {"ok": False, "message": "boom"}

    monkeypatch.setattr(verify, "_install_pinned_npm", _fake_install)

    code = verify.cmd_verify_pins(_args(json_mode=True, fix=True))
    assert code == verify.EXIT_USAGE
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["overall"] == "fix_failed"
    assert payload["failed_key"] == "mermaid_mcp"
    assert "boom" in payload["error"]


def test_manifest_load_failure_surfaces_cleanly(monkeypatch, capsys):
    def _explode():
        raise RuntimeError("manifest missing on disk")

    monkeypatch.setattr(verify, "_load_bundled_versions", _explode)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    code = verify.cmd_verify_pins(_args())
    assert code == verify.EXIT_DRIFT
    err = capsys.readouterr().err
    assert "cannot load bundled manifest" in err
    assert "manifest missing on disk" in err


def test_npm_view_subprocess_no_shell(monkeypatch):
    """Sanity-check: the subprocess invocation never uses ``shell=True``.

    Cross-OS rule from the plan + cross-os-hook-portability node.
    """
    captured: dict[str, Any] = {}

    class _FakeCompleted:
        stdout = json.dumps(
            {"dependencies": {"claude-mermaid": {"version": "1.4.2"}}}
        )
        stderr = ""
        returncode = 0

    def _fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeCompleted()

    monkeypatch.setattr(verify.subprocess, "run", _fake_run)
    result = verify._npm_view_version("claude-mermaid", npm_path="/usr/bin/npm")
    assert result == "1.4.2"
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 60
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    # First arg is the npm path, not a shell string.
    assert captured["args"][0] == "/usr/bin/npm"


def test_npm_view_timeout_returns_none(monkeypatch):
    def _fake_run(args, **kwargs):
        raise verify.subprocess.TimeoutExpired(cmd=args, timeout=60)

    monkeypatch.setattr(verify.subprocess, "run", _fake_run)
    result = verify._npm_view_version("claude-mermaid", npm_path="/usr/bin/npm")
    assert result is None


def test_npm_view_malformed_json_returns_none(monkeypatch):
    class _FakeCompleted:
        stdout = "not json at all"
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        verify.subprocess, "run", lambda *a, **k: _FakeCompleted()
    )
    result = verify._npm_view_version("claude-mermaid", npm_path="/usr/bin/npm")
    assert result is None


def test_pin_accessor_handles_dataclass(monkeypatch, capsys):
    """The accessor accepts both dict pins and dataclass pins.

    Phase 0.A may ship a NamedTuple or dataclass — verify our consumer
    survives either shape so the post-merge wiring is mechanical.
    """
    from types import SimpleNamespace

    pins = {
        "mermaid_mcp": SimpleNamespace(
            package="claude-mermaid", version="1.4.2", sha256="0" * 64
        ),
    }
    _stub_manifest(monkeypatch, pins)
    _stub_npm_path(monkeypatch, "/usr/bin/npm")
    _stub_npm_view(monkeypatch, {"claude-mermaid": "1.4.2"})
    code = verify.cmd_verify_pins(_args())
    assert code == verify.EXIT_OK
