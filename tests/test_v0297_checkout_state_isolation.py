# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W-CHECKOUT-STATE (v0.2.97): no test may write a checkout's own install log.

The incident: two tests drove install.py's soft-fail paths with
``PROJECT_ROOT`` left at the checkout, so ``_log_install_event`` appended
"codegraph identity sweep raised: boom" to ``<checkout>/state/logs/install.jsonl``.
In the public clone that directory does not exist (the event only buffers);
in an installed orchestrator root it does, and the line landed in a real
install log. ``tests/conftest.py`` now contains it in two layers; these tests
prove both, and that the log-asserting tests' own mechanism (``PROJECT_ROOT``
pointed at a tmp dir) still reaches the real function.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

import install


def _load_suite_conftest():
    """The already-imported ``tests/conftest.py`` — identified by file, because
    pytest's module name for it varies and a second copy would hold other state
    (the ``tests/test_v0292_user_state_isolation.py`` rule)."""
    wanted = (Path(__file__).parent / "conftest.py").resolve()
    for mod in list(sys.modules.values()):
        path = getattr(mod, "__file__", None)
        if path and Path(path).resolve() == wanted:
            return mod
    raise AssertionError(f"tests/conftest.py is not loaded ({wanted})")


_suite = _load_suite_conftest()
STATE_DIRS = _suite._CHECKOUT_STATE_DIRS


def _fake_install_module(checkout: Path) -> types.ModuleType:
    """An install.py-shaped module whose checkout HAS a ``state/logs`` dir —
    the installed-root case the public clone cannot show."""
    (checkout / "state" / "logs").mkdir(parents=True)
    module = types.ModuleType("install_fake_for_containment")
    module.__file__ = str(checkout / "install.py")
    module.PROJECT_ROOT = checkout

    def _install_log_path():
        log_dir = module.PROJECT_ROOT / "state" / "logs"
        return (log_dir / "install.jsonl") if log_dir.is_dir() else None

    module._install_log_path = _install_log_path
    return module


def test_the_loaded_install_module_is_contained():
    """Layer 1 is live for the real module every suite imports."""
    assert getattr(install._install_log_path, _suite._CONTAINED_MARK, False)
    assert install._install_log_path() is None, (
        "with PROJECT_ROOT at its own checkout, the log must look absent"
    )


def test_containment_hides_an_existing_checkout_log(tmp_path: Path):
    module = _fake_install_module(tmp_path / "checkout")
    assert module._install_log_path() is not None, "precondition: the dir exists"

    assert _suite.contain_install_module(module) is True
    assert module._install_log_path() is None
    assert _suite.contain_install_module(module) is False, "idempotent"


def test_a_tmp_project_root_still_reaches_the_real_log(tmp_path: Path):
    """The mechanism every log-asserting test uses is untouched."""
    module = _fake_install_module(tmp_path / "checkout")
    _suite.contain_install_module(module)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "state" / "logs").mkdir(parents=True)
    module.PROJECT_ROOT = elsewhere
    assert module._install_log_path() == elsewhere / "state" / "logs" / "install.jsonl"


def test_an_event_from_the_checkout_root_writes_nothing(monkeypatch):
    """End to end through the real ``_log_install_event``: buffered, not written."""
    monkeypatch.setattr(install, "_PENDING_EVENTS", [])
    install._log_install_event("w-checkout-state", "warn", "must not reach disk")
    assert len(install._PENDING_EVENTS) == 1
    assert _suite.consume_state_write_attempts() == []


@pytest.mark.parametrize("state_dir", STATE_DIRS, ids=lambda p: p.parent.name)
def test_a_write_under_a_checkout_state_dir_is_refused_and_recorded(state_dir: Path):
    """Layer 2: whatever the door, a write there is refused — and recorded, so a
    production soft-fail that swallows the refusal still reds the test."""
    target = state_dir / "logs" / "install.jsonl"
    existed = target.exists()
    with pytest.raises(_suite.RealUserStateWriteBlocked):
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("leak\n")
    probe = state_dir / "w-checkout-state-probe"
    with pytest.raises(_suite.RealUserStateWriteBlocked):
        os.makedirs(probe)
    attempts = _suite.consume_state_write_attempts()
    assert str(target) in attempts and str(probe) in attempts
    assert target.exists() == existed
    assert not probe.exists()


@pytest.mark.parametrize("state_dir", STATE_DIRS, ids=lambda p: p.parent.name)
def test_a_read_under_a_checkout_state_dir_is_not_refused(state_dir: Path):
    """Reading is harmless (the drift readers do it): only writes are refused."""
    missing = state_dir / "w-checkout-state-no-such-file"
    with pytest.raises(FileNotFoundError):
        with open(missing, encoding="utf-8"):
            pass
    assert _suite.consume_state_write_attempts() == []
