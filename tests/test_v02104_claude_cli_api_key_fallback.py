# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 API-1 — the Claude API-key retry of VCO's background ``claude -p`` jobs.

When the user's Claude subscription is out of usage, three background jobs
get ONE retry of the failed ``claude -p`` call with the shared Console key
``ANTHROPIC_API_KEY_GATEWAY``, passed as the child's ``ANTHROPIC_API_KEY``
environment variable only:

* the KG summary generator's CLI tier (``summary_backends.call_cli``),
* the code-summary generator (its CLI tier IS ``summary_backends.call_cli``
  through the shared ladder — pinned here by driving
  ``generate-code-summary.call_llm``),
* the post-commit KG reviewer (``kg_reviewer_backend.run_with_quota_fallback``,
  direct subscription rung only — a gateway rung's quota refusal is already
  retried server-side by the gateway's own fallback on the same switch).

All of it is gated on the launcher row ``model_gateway.api_key_fallback``
(the model gateway's own switch) and on the key resolving. The tests pin:
the retry fires ONLY on the existing quota classification; the key lives in
the retry child's env alone (never argv, never the parent env, never a log
line); fallback OFF / no key / a non-quota failure mean no retry; a refused
key is not retried twice; and the helper's two constants are the gateway's.

No network, no real secrets: the ``claude`` binary and ``subprocess.run``
are fakes, the key is an obvious placeholder.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import claude_cli_api_key as cak

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"

FAKE_KEY = "test-not-a-real-console-key"  # vco-secret-scan: test-placeholder

#: The exact log phrase the brief mandates (never with the key in it).
RETRY_PHRASE = "retrying with the Claude API key (ANTHROPIC_API_KEY_GATEWAY)"


def _load(name: str, path: Path) -> types.ModuleType:
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _ScriptedRun:
    """A fake ``subprocess.run`` answering from a script of outcomes.

    Every call is recorded (``argv`` + ``kwargs``); an Nth call beyond the
    script fails the test — that is how "no retry" is proven.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch,
                 outcomes: "list[types.SimpleNamespace]") -> None:
        self.calls: "list[dict]" = []
        self._outcomes = list(outcomes)

        def fake_run(argv, **kwargs):
            self.calls.append({"argv": list(argv), "kwargs": kwargs})
            if not self._outcomes:
                raise AssertionError(
                    f"unexpected spawn #{len(self.calls)}: {argv!r}")
            return self._outcomes.pop(0)

        monkeypatch.setattr(subprocess, "run", fake_run)


def _out(rc: int, stdout: str = "", stderr: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


QUOTA_FAILURE = _out(1, "", "Claude AI usage limit reached. Your limit will reset.")
AUTH_FAILURE = _out(1, "", "401 Unauthorized: invalid x-api-key")
SUCCESS = _out(0, "Resolves and caches the KG collection name.", "")


def _arm_fallback(monkeypatch: pytest.MonkeyPatch, *, enabled: bool,
                  key: "str | None") -> None:
    """Point the helper's two gates at the test's choice."""
    monkeypatch.setattr(cak, "fallback_enabled", lambda: enabled)
    monkeypatch.setattr(cak, "resolve_key", lambda: key)


def _assert_env_only(calls: "list[dict]", retried: bool) -> None:
    """The key may exist ONLY in the retry child's env: never argv, never
    the parent environment."""
    for call in calls:
        assert FAKE_KEY not in " ".join(call["argv"]), (
            "the key is on the command line")
    assert os.environ.get("ANTHROPIC_API_KEY") != FAKE_KEY, (
        "the parent environment was mutated")
    if retried:
        assert len(calls) == 2
        assert calls[1]["kwargs"]["env"].get("ANTHROPIC_API_KEY") == FAKE_KEY
        assert calls[0]["kwargs"]["env"].get("ANTHROPIC_API_KEY") != FAKE_KEY
    else:
        assert len(calls) == 1
        assert calls[0]["kwargs"]["env"].get("ANTHROPIC_API_KEY") != FAKE_KEY


def _assert_logs_clean(lines: "list[str]") -> None:
    assert any(RETRY_PHRASE in line for line in lines), (
        f"the retry log line is missing: {lines!r}")
    assert all(FAKE_KEY not in line for line in lines), (
        "a log line carries the key")


# ──────────────────────────────────────────────────────────────────────
# The helper itself
# ──────────────────────────────────────────────────────────────────────
class TestHelper:

    def test_fallback_enabled_reads_the_gateway_row(self, tmp_path: Path):
        db = make_launcher_db(
            tmp_path / "on.db",
            app_state={"model_gateway.api_key_fallback": "true"})
        assert cak.fallback_enabled(db_path=db) is True
        db1 = make_launcher_db(
            tmp_path / "on1.db",
            app_state={"model_gateway.api_key_fallback": "1"})
        assert cak.fallback_enabled(db_path=db1) is True

    def test_fallback_enabled_off_states(self, tmp_path: Path):
        off = make_launcher_db(
            tmp_path / "off.db",
            app_state={"model_gateway.api_key_fallback": "false"})
        assert cak.fallback_enabled(db_path=off) is False
        no_row = make_launcher_db(tmp_path / "norow.db")
        assert cak.fallback_enabled(db_path=no_row) is False
        assert cak.fallback_enabled(
            db_path=tmp_path / "missing.db") is False

    def test_fallback_enabled_unreadable_db_is_off(self, tmp_path: Path):
        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"this is not a sqlite database")
        assert cak.fallback_enabled(db_path=garbage) is False

    def test_resolve_key_uses_the_shared_slot_only(self, monkeypatch):
        seen: dict = {}

        def fake_get(key, **kwargs):
            seen["key"] = key
            seen["kwargs"] = kwargs
            return f"  {FAKE_KEY}\n"

        monkeypatch.setattr("vco_lib.agent_secrets.get", fake_get)
        assert cak.resolve_key() == FAKE_KEY
        assert seen["key"] == "ANTHROPIC_API_KEY_GATEWAY"
        assert seen["kwargs"]["shared_only"] is True
        assert seen["kwargs"]["allow_dotenv"] is False

    def test_resolve_key_never_raises(self, monkeypatch):
        from vco_lib.agent_secrets import SecretNotFound

        monkeypatch.setattr(
            "vco_lib.agent_secrets.get",
            lambda *a, **k: (_ for _ in ()).throw(SecretNotFound("nope")),
        )
        assert cak.resolve_key() is None
        monkeypatch.setattr(
            "vco_lib.agent_secrets.get",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert cak.resolve_key() is None
        monkeypatch.setattr("vco_lib.agent_secrets.get", lambda *a, **k: "  ")
        assert cak.resolve_key() is None

    def test_child_env_with_key(self):
        base = {
            "PATH": "/usr/bin",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:11460",
            "ANTHROPIC_AUTH_TOKEN": "gateway-token",
            "ANTHROPIC_API_KEY": "",
        }
        child = cak.child_env_with_key(base, FAKE_KEY)
        assert child["ANTHROPIC_API_KEY"] == FAKE_KEY
        # The gateway routing must go: with it, the retry would send the key
        # to the local gateway (which authenticates upstream itself) and
        # repeat the quota refusal it was meant to escape.
        assert "ANTHROPIC_BASE_URL" not in child
        assert "ANTHROPIC_AUTH_TOKEN" not in child
        assert child["PATH"] == "/usr/bin"
        # The input mapping is never mutated.
        assert base["ANTHROPIC_API_KEY"] == ""
        assert base["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11460"


# ──────────────────────────────────────────────────────────────────────
# Parity: the helper's constants are the gateway's (and the launcher's)
# ──────────────────────────────────────────────────────────────────────
class TestParity:

    def test_app_state_key_matches_the_gateway(self):
        from model_router.config import API_KEY_FALLBACK_APP_STATE_KEY

        assert cak.APP_STATE_KEY == API_KEY_FALLBACK_APP_STATE_KEY

    def test_secret_name_matches_the_gateway_route(self):
        from model_router.vendors import ANTHROPIC_API_KEY_ROUTE

        assert cak.SECRET_KEY_NAME in ANTHROPIC_API_KEY_ROUTE.secret_keys

    def test_app_state_key_matches_the_launcher(self):
        loaders = (REPO_ROOT / "launcher/src/lib/preferences/loaders.ts").read_text(
            encoding="utf-8")
        match = re.search(
            r"export const APP_STATE_KEY_GATEWAY_API_KEY_FALLBACK = '([^']+)'",
            loaders)
        assert match is not None, "the launcher constant is missing"
        assert match.group(1) == cak.APP_STATE_KEY


# ──────────────────────────────────────────────────────────────────────
# summary_backends.call_cli (the KG summary generator's CLI tier)
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture()
def sb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh summary_backends with isolated state and a captured logger."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct-state"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    mod = _load("_api1_sb", SCRIPTS_DIR / "summary_backends.py")
    mod.reset_backend_cache()
    mod.reset_breaker(persisted=True)
    lines: list[str] = []
    mod.set_logger(lines.append)
    mod.log_lines = lines  # type: ignore[attr-defined]  # dynamic test handle
    fake_cli = str(tmp_path / "claude")
    Path(fake_cli).write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(mod.shutil, "which", lambda name: fake_cli)
    # Skip the real `--help` probe: no isolation flags from a fake binary.
    mod._CLI_ISOLATION[fake_cli] = []
    return mod


class TestSummaryCliFallback:

    def test_quota_failure_retries_once_with_the_key_in_the_child_env(
            self, sb, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Claude AI usage limit reached. Your limit will reset."),
            SUCCESS,
        ])
        assert sb.call_cli("node body") == SUCCESS.stdout
        _assert_env_only(run.calls, retried=True)
        _assert_logs_clean(sb.log_lines)

    def test_fallback_off_means_no_retry(self, sb, monkeypatch):
        _arm_fallback(monkeypatch, enabled=False, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Claude AI usage limit reached."),
        ])
        with pytest.raises(sb.BackendUnavailable) as excinfo:
            sb.call_cli("node body")
        assert excinfo.value.reason == sb.REASON_QUOTA
        _assert_env_only(run.calls, retried=False)
        assert all(RETRY_PHRASE not in line for line in sb.log_lines)

    def test_no_key_means_no_retry(self, sb, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=None)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Claude AI usage limit reached."),
        ])
        with pytest.raises(sb.BackendUnavailable):
            sb.call_cli("node body")
        _assert_env_only(run.calls, retried=False)

    def test_a_non_quota_failure_is_not_retried(self, sb, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "overloaded_error: the service is at capacity"),
        ])
        with pytest.raises(sb.BackendUnavailable) as excinfo:
            sb.call_cli("node body")
        assert excinfo.value.reason == sb.REASON_CAPACITY
        _assert_env_only(run.calls, retried=False)

    def test_a_refused_key_is_not_retried_twice(self, sb, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [QUOTA_FAILURE, AUTH_FAILURE])
        with pytest.raises(sb.BackendUnavailable) as excinfo:
            sb.call_cli("node body")
        # The breaker must hear about the ORIGINAL quota failure: the tier
        # is out on the subscription, retried or not.
        assert excinfo.value.reason == sb.REASON_QUOTA
        assert len(run.calls) == 2
        _assert_logs_clean(sb.log_lines)
        assert any("refused" in line for line in sb.log_lines)

    def test_an_exit_zero_quota_notice_also_triggers_the_retry(
            self, sb, monkeypatch):
        """The CLI can print the usage-limit notice and exit 0 — the same
        quota classification, so the same one retry."""
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(0, "Claude AI usage limit reached. Your limit will reset.", ""),
            SUCCESS,
        ])
        assert sb.call_cli("node body") == SUCCESS.stdout
        _assert_env_only(run.calls, retried=True)


# ──────────────────────────────────────────────────────────────────────
# generate-code-summary: its CLI tier is the shared one
# ──────────────────────────────────────────────────────────────────────
class TestCodeSummaryCliFallback:

    def test_the_code_summary_cli_tier_gets_the_same_retry(
            self, sb, monkeypatch):
        """generate-code-summary has no ``claude -p`` spawn of its own: its
        ``call_llm`` descends the shared ladder, whose ``cli`` rung is
        ``summary_backends.call_cli``. Drive that path and the retry fires
        there too."""
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Claude AI usage limit reached."),
            SUCCESS,
        ])
        mod = _load("_api1_code_summary", SCRIPTS_DIR / "generate-code-summary.py")
        setattr(mod, "_sb", sb)  # dynamic test handle: the isolated ladder
        monkeypatch.setattr(sb, "select_backend", lambda **kwargs: "cli")
        monkeypatch.setattr(sb, "breaker_state", lambda tier: None)
        monkeypatch.setattr(sb, "clear_backend_trip", lambda tier: None)
        assert mod.call_llm("code entity") == SUCCESS.stdout
        _assert_env_only(run.calls, retried=True)


# ──────────────────────────────────────────────────────────────────────
# kg_reviewer_backend.run_with_quota_fallback (the post-commit reviewer)
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture()
def reviewer(sb, monkeypatch: pytest.MonkeyPatch):
    mod = _load("_api1_kg_reviewer", SCRIPTS_DIR / "kg_reviewer_backend.py")
    setattr(mod, "_sb", sb)  # dynamic test handle: the isolated breaker home
    return mod


class TestKgReviewerFallback:

    CMD = ["/fake/claude", "-p", "--model", "haiku"]

    def _run(self, reviewer, run, *, direct=True, env=None, log_lines=None):
        return reviewer.run_with_quota_fallback(
            self.CMD, "the prompt",
            env if env is not None else {"PATH": "/usr/bin"},
            Path("/tmp"), 60,
            direct_rung=direct,
            log=(log_lines if log_lines is not None else []).append)

    def test_quota_failure_retries_once_with_the_key_in_the_child_env(
            self, reviewer, monkeypatch, tmp_path):
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Claude AI usage limit reached."),
            _out(0, '{"type": "result", "result": "reviewed", "num_turns": 4}', ""),
        ])
        lines: list[str] = []
        env = {
            "PATH": "/usr/bin",
            # What a hook-spawned reviewer inherits on a gateway-pointed
            # machine: the retry must drop these or the key goes to the
            # gateway and the quota refusal repeats.
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:11460",
            "ANTHROPIC_AUTH_TOKEN": "gateway-token",
        }
        stdout, _stderr, exit_code = reviewer.run_with_quota_fallback(
            self.CMD, "the prompt", env, tmp_path, 60,
            direct_rung=True, log=lines.append)
        assert exit_code == 0 and "reviewed" in stdout
        _assert_env_only(run.calls, retried=True)
        retry_env = run.calls[1]["kwargs"]["env"]
        assert "ANTHROPIC_BASE_URL" not in retry_env
        assert "ANTHROPIC_AUTH_TOKEN" not in retry_env
        # The caller's env mapping is untouched by the retry.
        assert env == {"PATH": "/usr/bin",
                       "ANTHROPIC_BASE_URL": "http://127.0.0.1:11460",
                       "ANTHROPIC_AUTH_TOKEN": "gateway-token"}
        _assert_logs_clean(lines)

    def test_a_gateway_rung_is_not_retried(self, reviewer, monkeypatch):
        """A gateway rung's quota refusal is the GATEWAY's fallback to make
        (same switch, server-side); its claude-gw/ model ids are not served
        by api.anthropic.com anyway."""
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Claude AI usage limit reached."),
        ])
        lines: list[str] = []
        _stdout, _stderr, exit_code = self._run(
            reviewer, run, direct=False, log_lines=lines)
        assert exit_code == 1
        _assert_env_only(run.calls, retried=False)
        assert all(RETRY_PHRASE not in line for line in lines)

    def test_fallback_off_means_no_retry(self, reviewer, monkeypatch):
        _arm_fallback(monkeypatch, enabled=False, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Claude AI usage limit reached."),
        ])
        self._run(reviewer, run)
        _assert_env_only(run.calls, retried=False)

    def test_no_key_means_no_retry(self, reviewer, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=None)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Claude AI usage limit reached."),
        ])
        self._run(reviewer, run)
        _assert_env_only(run.calls, retried=False)

    def test_a_non_quota_failure_is_not_retried(self, reviewer, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "", "Error: the model exploded for a reason the "
                        "classifier reads as unknown"),
        ])
        self._run(reviewer, run)
        _assert_env_only(run.calls, retried=False)

    def test_a_served_run_is_not_retried(self, reviewer, monkeypatch):
        """Exit non-zero but the backend answered every turn (the client's
        own max-turns budget): not a backend failure, no retry."""
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [
            _out(1, "Error: Reached max turns (30)\n", ""),
        ])
        self._run(reviewer, run)
        _assert_env_only(run.calls, retried=False)

    def test_a_refused_key_is_not_retried_twice(self, reviewer, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [QUOTA_FAILURE, AUTH_FAILURE])
        lines: list[str] = []
        _stdout, _stderr, exit_code = self._run(
            reviewer, run, log_lines=lines)
        assert exit_code == 1
        assert len(run.calls) == 2
        _assert_logs_clean(lines)
        assert any("refused" in line for line in lines)
