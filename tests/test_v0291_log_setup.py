# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 Decision #21 — ``vco_lib.log_setup`` (the Python-side consumer of
the global ``logging.level`` preference).

Pre-fix, every Python entry point that called
``logging.basicConfig(level=logging.INFO)`` hardcoded INFO and ignored the
new ``VCO_LOG_LEVEL`` env var vct-hub projects from the ``logging.level``
global pref (values ``error|warn|info|debug``, case-insensitive; the env
var may be absent). This module is the ONE shared helper
(``vco_lib.log_setup.configure_logging``) all of those call sites now route
through.

Covered here:
* ``VCO_LOG_LEVEL`` valid at each of the 4 documented tiers, mixed case.
* Invalid value falls through to the caller's ``default``.
* Absent env var falls through to the caller's ``default``.
* Idempotence — calling ``configure_logging`` twice does not raise and both
  calls cleanly delegate to ``logging.basicConfig`` (the actual
  no-op-once-handlers-exist behaviour is stdlib's OWN documented contract;
  verified end-to-end in a subprocess, where no other handler is present).
* A source-scan guard: no bare ``logging.basicConfig(level=`` call remains
  anywhere under ``vco_lib/`` or ``claude_mcp_servers/`` OUTSIDE
  ``vco_lib/log_setup.py`` itself. The scanner asserts it FINDS
  ``log_setup.py``'s own ``logging.basicConfig(level=level, ...)`` call so a
  broken/over-eager exclusion can't make the assertion vacuously pass.

Test-design note: the ``configure_logging`` tests assert on the RESOLVED
kwargs passed to ``logging.basicConfig`` (via a monkeypatched stand-in)
rather than on live ``logging.getLogger().handlers`` / ``.level`` state.
pytest's own log-capture plugin (``_pytest.logging``) re-attaches a fresh
``LogCaptureHandler`` to the root logger around each test phase
(``catching_logs``), so by the time a test body runs, ``root.handlers`` is
never actually empty — asserting on that state is inherently flaky /
plugin-version-dependent, and directly caused early failures during
authoring (basicConfig's own no-op-if-handlers-exist branch silently ate
every assertion because pytest's handler was already installed). Two
subprocess-based tests below cover the true end-to-end wiring (a fresh
interpreter has no pytest handler on its root logger).

Scope note: this governs diagnostics only. rl_events / rl_retention /
rl_logger are explicitly OUT of scope for this pref (data, not diagnostics)
and are not touched by this test file or by ``log_setup.py``.
"""
from __future__ import annotations

import importlib
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

from vco_lib import log_setup

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def basic_config_calls(monkeypatch):
    """Record every ``logging.basicConfig(**kwargs)`` call ``configure_logging``
    makes, without touching the real root logger. See the module docstring's
    "Test-design note" for why live root-logger state isn't asserted on
    directly under pytest.
    """
    calls = []
    monkeypatch.setattr(log_setup.logging, "basicConfig", lambda **kw: calls.append(kw))
    return calls


def _run_in_subprocess(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# resolve_level — pure function, no root-logger side effects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("error", logging.ERROR),
        ("ERROR", logging.ERROR),
        ("Error", logging.ERROR),
        ("warn", logging.WARNING),
        ("WARN", logging.WARNING),
        ("WaRn", logging.WARNING),
        ("info", logging.INFO),
        ("INFO", logging.INFO),
        ("debug", logging.DEBUG),
        ("DEBUG", logging.DEBUG),
        ("DeBuG", logging.DEBUG),
    ],
)
def test_resolve_level_valid_each_tier_mixed_case(monkeypatch, raw, expected):
    monkeypatch.setenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, raw)
    assert log_setup.resolve_level(default=logging.INFO) == expected
    # A non-INFO default must not leak through when the env var is valid —
    # proves the env value wins over the default, not the other way round.
    assert log_setup.resolve_level(default=logging.CRITICAL) == expected


@pytest.mark.parametrize("raw", ["verbose", "trace", "notset", "", "  ", "1", "info!"])
def test_resolve_level_invalid_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, raw)
    assert log_setup.resolve_level(default=logging.WARNING) == logging.WARNING
    assert log_setup.resolve_level(default=logging.DEBUG) == logging.DEBUG


def test_resolve_level_absent_falls_back_to_default(monkeypatch):
    monkeypatch.delenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, raising=False)
    assert log_setup.resolve_level(default=logging.INFO) == logging.INFO
    assert log_setup.resolve_level(default=logging.ERROR) == logging.ERROR


def test_resolve_level_default_is_info_when_unspecified(monkeypatch):
    monkeypatch.delenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, raising=False)
    assert log_setup.resolve_level() == logging.INFO


# ---------------------------------------------------------------------------
# configure_logging — wires resolve_level into logging.basicConfig
# ---------------------------------------------------------------------------

def test_configure_logging_valid_env_resolves_and_calls_basicconfig(monkeypatch, basic_config_calls):
    monkeypatch.setenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, "debug")
    log_setup.configure_logging(default=logging.INFO)
    assert basic_config_calls == [{"level": logging.DEBUG}]


def test_configure_logging_invalid_env_falls_back_to_default(monkeypatch, basic_config_calls):
    monkeypatch.setenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, "not-a-level")
    log_setup.configure_logging(default=logging.WARNING)
    assert basic_config_calls == [{"level": logging.WARNING}]


def test_configure_logging_absent_env_falls_back_to_default(monkeypatch, basic_config_calls):
    monkeypatch.delenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, raising=False)
    log_setup.configure_logging(default=logging.INFO)
    assert basic_config_calls == [{"level": logging.INFO}]


def test_configure_logging_forwards_format_kwarg_like_call_sites_did(monkeypatch, basic_config_calls):
    """Migrated call sites (e.g. codegraph_vector_copy.py, mermaid_proxy.py)
    pass a custom ``format=`` string through unchanged — verify the kwarg
    passthrough reaches ``logging.basicConfig`` verbatim, alongside the
    resolved level.
    """
    monkeypatch.delenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, raising=False)
    log_setup.configure_logging(
        default=logging.INFO, format="[test-proxy] %(levelname)s %(message)s"
    )
    assert basic_config_calls == [
        {"level": logging.INFO, "format": "[test-proxy] %(levelname)s %(message)s"}
    ]


def test_configure_logging_idempotent_second_call_still_delegates(monkeypatch, basic_config_calls):
    """``configure_logging`` must not grow its own "already configured"
    tracking that would prevent a legitimate second call from reaching
    ``logging.basicConfig`` — that would break the ``force=True`` escape
    hatch stdlib callers rely on. The actual no-op-if-handlers-exist
    behaviour is stdlib's own contract (verified end-to-end in
    ``test_configure_logging_idempotent_in_subprocess`` below); this test's
    job is only to prove the wrapper is a faithful passthrough on repeat
    calls, safe to call twice without raising.
    """
    monkeypatch.delenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, raising=False)
    log_setup.configure_logging(default=logging.INFO)

    monkeypatch.setenv(log_setup.VCO_LOG_LEVEL_ENV_VAR, "debug")
    log_setup.configure_logging(default=logging.ERROR, force=True)

    assert basic_config_calls == [
        {"level": logging.INFO},
        {"level": logging.DEBUG, "force": True},
    ]


def test_configure_logging_real_basicconfig_sets_level_in_subprocess():
    """End-to-end check in a FRESH interpreter (no pytest log-capture
    handler pre-attached to the root logger) that ``configure_logging``'s
    delegation to the real ``logging.basicConfig`` actually sets the level.
    """
    script = (
        "import logging, os; "
        "os.environ['VCO_LOG_LEVEL'] = 'debug'; "
        "from vco_lib.log_setup import configure_logging; "
        "configure_logging(default=logging.INFO); "
        "lvl = logging.getLogger().level; "
        "assert lvl == logging.DEBUG, lvl; "
        "print('OK')"
    )
    result = _run_in_subprocess(script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_configure_logging_idempotent_in_subprocess():
    """End-to-end idempotence: in a fresh interpreter with no pre-existing
    root handler, a second ``configure_logging`` call (no ``force=True``)
    must be a genuine no-op — same handler count, same level as the first
    call — matching stdlib ``logging.basicConfig``'s documented contract.
    """
    script = (
        "import logging, os; "
        "os.environ.pop('VCO_LOG_LEVEL', None); "
        "from vco_lib.log_setup import configure_logging; "
        "configure_logging(default=logging.INFO); "
        "root = logging.getLogger(); "
        "n1, lvl1 = len(root.handlers), root.level; "
        "os.environ['VCO_LOG_LEVEL'] = 'debug'; "
        "configure_logging(default=logging.ERROR); "
        "n2, lvl2 = len(root.handlers), root.level; "
        "assert (n1, lvl1) == (n2, lvl2) and n1 >= 1 and lvl1 == logging.INFO, (n1, lvl1, n2, lvl2); "
        "print('OK')"
    )
    result = _run_in_subprocess(script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Closure guard — no stray hardcoded basicConfig(level=...) left behind
# ---------------------------------------------------------------------------

# Matches `logging.basicConfig(level=` with any amount of whitespace/newline
# between `basicConfig(` and `level=`, since some pre-migration call sites
# split the call across lines (e.g. codegraph_vector_copy.py had
# `logging.basicConfig(\n    level=logging.INFO, format=...\n)`).
_BASICCONFIG_LEVEL_RE = re.compile(r"logging\.basicConfig\(\s*level\s*=", re.MULTILINE)

_SCAN_DIRS = ("vco_lib", "claude_mcp_servers")
_SELF_EXEMPT_PATH = Path("vco_lib") / "log_setup.py"


def _iter_python_files():
    for scan_dir in _SCAN_DIRS:
        yield from (REPO_ROOT / scan_dir).rglob("*.py")


def test_no_bare_basicconfig_level_outside_log_setup():
    """Source-scan: after the v0.2.91 migration, every hardcoded
    ``logging.basicConfig(level=...)`` call under vco_lib/ or
    claude_mcp_servers/ must be gone EXCEPT inside log_setup.py itself
    (which legitimately wraps basicConfig).
    """
    offenders = []
    found_self = False
    for path in _iter_python_files():
        rel = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        if not _BASICCONFIG_LEVEL_RE.search(text):
            continue
        if rel == _SELF_EXEMPT_PATH:
            found_self = True
            continue
        offenders.append(str(rel))

    # Self-check: a scanner that can't even find log_setup.py's OWN
    # `logging.basicConfig(level=level, ...)` call is broken (e.g. wrong
    # regex, wrong scan root) and must not be allowed to pass vacuously.
    assert found_self, (
        "scanner sanity check failed: did not find vco_lib/log_setup.py's "
        "own logging.basicConfig(level=...) call — the regex or scan root "
        "is broken, so a real offender could slip through undetected"
    )
    assert offenders == [], (
        "hardcoded logging.basicConfig(level=...) found outside "
        f"log_setup.py (must route through configure_logging instead): {offenders}"
    )


def test_log_setup_module_importable_standalone():
    """Sanity: the module has no import-time side effects that would break
    a plain `import vco_lib.log_setup` (e.g. no accidental basicConfig call
    at module scope — only inside configure_logging()).
    """
    reloaded = importlib.import_module("vco_lib.log_setup")
    assert hasattr(reloaded, "configure_logging")
    assert hasattr(reloaded, "resolve_level")
