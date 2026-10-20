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
line); fallback OFF / no key / a non-quota failure / a foreign routing env
mean no retry; a refused key is not retried twice and, in the reviewer,
latches the direct rung on QUOTA (5 h); after a served retry the rest of a
summary process goes straight to the key (one resolve, one subscription
spawn); the key resolves from VCO's shared slot only, on this helper's path
AND the model gateway's; and the helper's constants are the gateway's.

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

    def test_child_env_with_key(self, monkeypatch):
        monkeypatch.setenv("VCT_MODEL_GATEWAY_PORT", "11460")
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

        # The gateway reads secret_keys[0] (api_key_route.py) — pin the one
        # name, not mere membership.
        assert ANTHROPIC_API_KEY_ROUTE.secret_keys == (cak.SECRET_KEY_NAME,)

    def test_the_hub_declares_the_key_slot_exclusive(self):
        """LOW-1 (hub tier): ``vct-module.json`` declares VCO's key as a
        shared, ``slot_exclusive`` bundled secret — the hub's ``/env`` then
        serves it ONLY from the shared slot (``slot_exclusive_keys`` in
        ``vct-hub/src/modules_api.rs``; its Rust tests pin the refusal of a
        per-project stand-in)."""
        import json

        manifest = json.loads((REPO_ROOT / "vct-module.json").read_text(
            encoding="utf-8"))
        rows = [s for s in manifest["bundled_secrets"]
                if s["key"] == cak.SECRET_KEY_NAME]
        assert len(rows) == 1
        assert rows[0]["scope"] == "shared"
        assert rows[0]["module_id"] == "user"
        assert rows[0]["slot_exclusive"] is True

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
    # Hermetic routing: the machine running the suite may point its own
    # Claude at a gateway or a cloud provider; the retry decision reads the
    # job's env (``claude_cli_api_key.retry_blocker``).
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                 "ANTHROPIC_AUTH_TOKEN", *cak.PROVIDER_SWITCHES):
        monkeypatch.delenv(name, raising=False)
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
        monkeypatch.setenv("VCT_MODEL_GATEWAY_PORT", "11460")
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
        run_result = reviewer.run_with_quota_fallback(
            self.CMD, "the prompt", env, tmp_path, 60,
            direct_rung=True, log=lines.append)
        assert run_result.exit_code == 0 and "reviewed" in run_result.stdout
        # A served retry is what the breaker records (it clears the rung).
        assert run_result.record[2] == 0
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
        result = self._run(reviewer, run, direct=False, log_lines=lines)
        assert result.exit_code == 1
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
        result = self._run(reviewer, run, log_lines=lines)
        assert result.exit_code == 1
        assert len(run.calls) == 2
        _assert_logs_clean(lines)
        assert any("refused" in line for line in lines)
        # MEDIUM-1: the breaker classifies the ORIGINAL subscription run.
        assert result.record == (QUOTA_FAILURE.stdout, QUOTA_FAILURE.stderr,
                                 QUOTA_FAILURE.returncode)


# ──────────────────────────────────────────────────────────────────────
# R21 fix round — MEDIUM-1: the reviewer latches QUOTA after a failed retry
# ──────────────────────────────────────────────────────────────────────
_REAL_RUN = subprocess.run


def _git(repo: Path, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, check=True,
                     capture_output=True, text=True).stdout


@pytest.fixture()
def review_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "proj"
    (repo / "knowledge" / "concepts").mkdir(parents=True)
    (repo / "knowledge" / "concepts" / "a.md").write_text(
        "---\ntitle: A\ntype: concept\n---\n\nBody.\n", encoding="utf-8")
    (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "src.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feat: bump x")
    return repo


class TestReviewerLatchAfterFailedRetry:
    """End to end through ``kg_reviewer_backend.main(["run", ...])``: the
    breaker's latch after a quota refusal whose key retry did not serve is
    QUOTA (the 5 h cooldown), so the next commits inside the window spawn
    neither the subscription nor the refused key."""

    def _drive(self, reviewer, monkeypatch, repo: Path, retry) -> "list[list]":
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        spawns: "list[list]" = []

        def run(cmd, *a, **kw):
            if cmd and cmd[0] == "claude":
                spawns.append(list(cmd))
                if len(spawns) == 1:
                    return subprocess.CompletedProcess(
                        cmd, QUOTA_FAILURE.returncode,
                        stdout=QUOTA_FAILURE.stdout, stderr=QUOTA_FAILURE.stderr)
                return retry(cmd, kw)
            return _REAL_RUN(cmd, *a, **kw)

        monkeypatch.setattr(reviewer, "subprocess", types.SimpleNamespace(
            run=run, TimeoutExpired=subprocess.TimeoutExpired,
            CompletedProcess=subprocess.CompletedProcess,
            DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE))
        monkeypatch.setattr(reviewer, "resolve_claude_executable",
                            lambda: "claude")
        monkeypatch.setattr(reviewer, "select_reviewer_backend",
                            lambda log=None, model=None: {
                                "mode": "claude", "model": "sonnet",
                                "base_url": None, "reason": "t"})
        log = repo.parent / "review.log"
        reviewer.main(["run", "--project-root", str(repo), "--log", str(log)])
        return spawns

    def _latch(self, reviewer):
        return reviewer._sb.breaker_state(reviewer._tier_for("sonnet"))

    def test_refused_key_latches_quota(self, reviewer, monkeypatch, review_repo):
        spawns = self._drive(
            reviewer, monkeypatch, review_repo,
            lambda cmd, kw: subprocess.CompletedProcess(
                cmd, AUTH_FAILURE.returncode, stdout=AUTH_FAILURE.stdout,
                stderr=AUTH_FAILURE.stderr))
        assert len(spawns) == 2
        latch = self._latch(reviewer)
        assert latch is not None
        assert latch["reason"] == reviewer._sb.REASON_QUOTA
        assert reviewer._sb.cooldown_for(latch["reason"]) == 18000.0
        assert latch["remaining"] > 120
        # The next commit inside the window: the direct rung is latched, so
        # neither the subscription nor the refused key is spawned.
        assert reviewer._claude_model_available("sonnet", lambda _l: None) is False

    def test_retry_timeout_latches_quota_without_a_traceback(
            self, reviewer, monkeypatch, review_repo):
        def timeout(cmd, kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 1,
                                            output=b"", stderr=b"")

        spawns = self._drive(reviewer, monkeypatch, review_repo, timeout)
        assert len(spawns) == 2
        latch = self._latch(reviewer)
        assert latch is not None
        assert latch["reason"] == reviewer._sb.REASON_QUOTA
        text = (review_repo.parent / "review.log").read_text(encoding="utf-8")
        assert "Traceback" not in text
        assert "TIMED OUT" in text

    def test_retry_timeout_inside_the_helper(self, reviewer, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        calls: list = []

        def fake_run(argv, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return QUOTA_FAILURE
            raise subprocess.TimeoutExpired(argv, 60, output=b"partial",
                                            stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        lines: list[str] = []
        result = reviewer.run_with_quota_fallback(
            ["/fake/claude", "-p"], "p", {"PATH": "/usr/bin"}, Path("/tmp"), 60,
            direct_rung=True, log=lines.append)
        assert result.exit_code == 124
        assert result.stdout == "partial"  # the retry's writes stay judgeable
        assert result.record[2] == QUOTA_FAILURE.returncode
        assert any("TIMED OUT" in line for line in lines)


# ──────────────────────────────────────────────────────────────────────
# R21 fix round — MEDIUM-2: after a served key retry, the process uses the key
# ──────────────────────────────────────────────────────────────────────
class TestSummaryKeyFirstMemo:

    def _arm_counted(self, monkeypatch, *, key=FAKE_KEY) -> dict:
        counts = {"enabled": 0, "resolve": 0}

        def enabled():
            counts["enabled"] += 1
            return True

        def resolve():
            counts["resolve"] += 1
            return key

        monkeypatch.setattr(cak, "fallback_enabled", enabled)
        monkeypatch.setattr(cak, "resolve_key", resolve)
        return counts

    def test_n_nodes_after_a_quota_trip_cost_one_subscription_spawn(
            self, sb, monkeypatch):
        counts = self._arm_counted(monkeypatch)
        monkeypatch.setattr(sb, "select_backend", lambda **kw: "cli")
        n = 4
        run = _ScriptedRun(monkeypatch, [QUOTA_FAILURE] + [SUCCESS] * n)
        for _ in range(n):
            assert sb.call_llm("node") == SUCCESS.stdout
        subscription = [c for c in run.calls
                        if c["kwargs"]["env"].get("ANTHROPIC_API_KEY") != FAKE_KEY]
        keyed = [c for c in run.calls
                 if c["kwargs"]["env"].get("ANTHROPIC_API_KEY") == FAKE_KEY]
        assert len(subscription) == 1
        assert len(keyed) == n
        assert counts["resolve"] == 1
        assert counts["enabled"] == 1
        assert sb.breaker_state("cli") is None
        assert all(FAKE_KEY not in line for line in sb.log_lines)
        assert all(FAKE_KEY not in " ".join(c["argv"]) for c in run.calls)

    def test_a_key_refusal_ends_key_first_and_falls_back_to_the_subscription(
            self, sb, monkeypatch):
        counts = self._arm_counted(monkeypatch)
        run = _ScriptedRun(monkeypatch, [
            QUOTA_FAILURE, SUCCESS,        # node 1: subscription out, key serves
            AUTH_FAILURE, QUOTA_FAILURE,   # node 2: key refused -> subscription
        ])
        assert sb.call_cli("node 1") == SUCCESS.stdout
        with pytest.raises(sb.BackendUnavailable) as excinfo:
            sb.call_cli("node 2")
        # The subscription's quota refusal is what the breaker hears.
        assert excinfo.value.reason == sb.REASON_QUOTA
        envs = [c["kwargs"]["env"].get("ANTHROPIC_API_KEY") for c in run.calls]
        assert envs[1] == FAKE_KEY and envs[2] == FAKE_KEY
        assert envs[3] != FAKE_KEY  # no second key spawn after the refusal
        assert counts["resolve"] == 1
        # And no further key spawn in this process.
        _ScriptedRun(monkeypatch, [QUOTA_FAILURE])
        with pytest.raises(sb.BackendUnavailable):
            sb.call_cli("node 3")

    def test_a_transient_key_failure_keeps_key_first(self, sb, monkeypatch):
        self._arm_counted(monkeypatch)
        run = _ScriptedRun(monkeypatch, [
            QUOTA_FAILURE, SUCCESS,
            _out(1, "", "overloaded_error: the service is at capacity"),
            SUCCESS,
        ])
        assert sb.call_cli("node 1") == SUCCESS.stdout
        with pytest.raises(sb.BackendUnavailable) as excinfo:
            sb.call_cli("node 2")
        assert excinfo.value.reason == sb.REASON_CAPACITY
        assert sb.call_cli("node 3") == SUCCESS.stdout
        assert all(c["kwargs"]["env"].get("ANTHROPIC_API_KEY") == FAKE_KEY
                   for c in run.calls[1:])

    def test_the_memo_is_process_local(self, sb, monkeypatch):
        """A fresh process (simulated by ``reset_backend_cache``, which the
        generators call at import) re-probes the subscription first."""
        self._arm_counted(monkeypatch)
        run = _ScriptedRun(monkeypatch, [QUOTA_FAILURE, SUCCESS, SUCCESS])
        assert sb.call_cli("node 1") == SUCCESS.stdout
        isolation = dict(sb._CLI_ISOLATION)
        sb.reset_backend_cache()
        sb._CLI_ISOLATION.update(isolation)  # skip the fake binary's --help
        assert sb.call_cli("node 2") == SUCCESS.stdout
        assert run.calls[2]["kwargs"]["env"].get("ANTHROPIC_API_KEY") != FAKE_KEY


# ──────────────────────────────────────────────────────────────────────
# R21 fix round — LOW-1: VCO's own slot only, on both paths
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture()
def project_with_stand_ins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A project that holds ITS OWN secret under VCO's name in every
    project-scoped store the chain could read: the file store's project
    leg and the project ``.env``. The hub tier is down (its slot-exclusive
    rule is pinned by the manifest test and the hub's Rust tests)."""
    from vco_lib import agent_secrets
    from vco_lib.project_config import HubUnreachable

    store = tmp_path / "vct-secrets"
    (store / "projects" / "proj").mkdir(parents=True)
    (store / "projects" / "proj" / cak.SECRET_KEY_NAME).write_text(
        "project-stand-in\n", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".vct-project").write_text("proj\n", encoding="utf-8")
    (project / ".env").write_text(
        f"{cak.SECRET_KEY_NAME}=project-dotenv-stand-in\n", encoding="utf-8")
    monkeypatch.setattr(agent_secrets, "_secrets_root", lambda: store)

    def hub_down(key, project=None):
        raise HubUnreachable("hub down in this test")

    monkeypatch.setattr(agent_secrets, "_hub_get", hub_down)
    monkeypatch.chdir(project)
    return store, project


class TestVcoSlotOnly:

    def test_the_helper_never_takes_a_projects_secret(
            self, project_with_stand_ins):
        store, _project = project_with_stand_ins
        assert cak.resolve_key() is None
        (store / "shared").mkdir()
        (store / "shared" / cak.SECRET_KEY_NAME).write_text(
            FAKE_KEY + "\n", encoding="utf-8")
        assert cak.resolve_key() == FAKE_KEY

    def test_the_gateway_never_takes_a_projects_secret(
            self, project_with_stand_ins):
        from model_router.secrets import VendorKeyResolver
        from model_router.vendors import ANTHROPIC_API_KEY_ROUTE

        from vco_lib.agent_secrets import get as real_get

        store, project = project_with_stand_ins
        # Scoped AS the project (the VCT_MODEL_GATEWAY_SECRET_PROJECT shape):
        # a vendor row would take that project's key; VCO's own may not.
        resolver = VendorKeyResolver(project=str(project), getter=real_get)
        assert resolver.resolve(ANTHROPIC_API_KEY_ROUTE).key is None
        (store / "shared").mkdir()
        (store / "shared" / cak.SECRET_KEY_NAME).write_text(
            FAKE_KEY + "\n", encoding="utf-8")
        resolver = VendorKeyResolver(project=str(project), getter=real_get)
        assert resolver.resolve(ANTHROPIC_API_KEY_ROUTE).key == FAKE_KEY

    def test_a_vendor_row_keeps_its_project_override(self):
        """The shared-only rule is the API-key route's, not every vendor's."""
        from model_router.secrets import VendorKeyResolver
        from model_router.vendors import VENDORS

        seen: dict = {}

        def getter(name, project=None, **kwargs):
            seen["kwargs"] = kwargs
            return "vendor-key-placeholder"

        VendorKeyResolver(project="/x", getter=getter).resolve(VENDORS["zai"])
        assert seen["kwargs"] == {}


# ──────────────────────────────────────────────────────────────────────
# R21 fix round — LOW-2: foreign routing is left alone
# ──────────────────────────────────────────────────────────────────────
class TestRetryBlocker:

    @pytest.mark.parametrize("switch", cak.PROVIDER_SWITCHES)
    def test_a_provider_switch_blocks_the_retry(self, switch):
        why = cak.retry_blocker({switch: "1"})
        assert why is not None and switch in why

    @pytest.mark.parametrize("value", ["", "0", "false", "OFF"])
    def test_a_switch_set_off_does_not_block(self, value):
        assert cak.retry_blocker({"CLAUDE_CODE_USE_BEDROCK": value}) is None

    def test_a_foreign_base_url_blocks_and_is_not_echoed(self, monkeypatch):
        monkeypatch.setenv("VCT_MODEL_GATEWAY_PORT", "11460")
        url = "https://user:secret-in-url@proxy.example.invalid/anthropic"
        why = cak.retry_blocker({"ANTHROPIC_BASE_URL": url})
        assert why is not None and "ANTHROPIC_BASE_URL" in why
        assert "secret-in-url" not in why and "proxy.example" not in why
        # A loopback URL on a port that is NOT the gateway's is foreign too.
        assert cak.retry_blocker(
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9999"}) is not None

    def test_the_vco_gateway_url_does_not_block_and_is_stripped(
            self, monkeypatch):
        monkeypatch.setenv("VCT_MODEL_GATEWAY_PORT", "11460")
        env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11460",
               "ANTHROPIC_AUTH_TOKEN": "gateway-token"}
        assert cak.retry_blocker(env) is None
        child = cak.child_env_with_key(env, FAKE_KEY)
        assert "ANTHROPIC_BASE_URL" not in child
        assert "ANTHROPIC_AUTH_TOKEN" not in child

    def test_a_foreign_base_url_is_never_stripped(self, monkeypatch):
        monkeypatch.setenv("VCT_MODEL_GATEWAY_PORT", "11460")
        env = {"ANTHROPIC_BASE_URL": "https://proxy.example.invalid"}
        assert cak.child_env_with_key(env, FAKE_KEY)["ANTHROPIC_BASE_URL"] == (
            "https://proxy.example.invalid")

    def test_a_users_own_auth_token_blocks(self):
        assert cak.retry_blocker({"ANTHROPIC_AUTH_TOKEN": "t"}) is not None

    def test_a_blocked_env_logs_one_line_and_never_resolves(self, monkeypatch):
        resolved: list = []
        monkeypatch.setattr(cak, "fallback_enabled", lambda: True)
        monkeypatch.setattr(cak, "resolve_key",
                            lambda: resolved.append(1) or FAKE_KEY)
        lines: list[str] = []
        assert cak.retry_env({"CLAUDE_CODE_USE_VERTEX": "1"},
                             lines.append) is None
        assert resolved == []
        assert len(lines) == 1 and "CLAUDE_CODE_USE_VERTEX" in lines[0]

    def test_the_summary_job_skips_the_retry_under_bedrock(
            self, sb, monkeypatch):
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        run = _ScriptedRun(monkeypatch, [QUOTA_FAILURE])
        with pytest.raises(sb.BackendUnavailable):
            sb.call_cli("node")
        _assert_env_only(run.calls, retried=False)
        assert any("CLAUDE_CODE_USE_BEDROCK" in line for line in sb.log_lines)

    def test_the_reviewer_skips_the_retry_behind_a_foreign_proxy(
            self, reviewer, monkeypatch):
        monkeypatch.setenv("VCT_MODEL_GATEWAY_PORT", "11460")
        _arm_fallback(monkeypatch, enabled=True, key=FAKE_KEY)
        run = _ScriptedRun(monkeypatch, [QUOTA_FAILURE])
        lines: list[str] = []
        reviewer.run_with_quota_fallback(
            ["/fake/claude", "-p"], "p",
            {"PATH": "/usr/bin",
             "ANTHROPIC_BASE_URL": "https://proxy.example.invalid"},
            Path("/tmp"), 60, direct_rung=True, log=lines.append)
        _assert_env_only(run.calls, retried=False)
        assert any("ANTHROPIC_BASE_URL" in line for line in lines)


# ──────────────────────────────────────────────────────────────────────
# R21 fix round — LOW-3: a broken install is said, not swallowed
# ──────────────────────────────────────────────────────────────────────
class TestBrokenInstallIsLoud:

    def test_import_error_prints_one_stderr_line(self, sb, monkeypatch, capsys):
        import builtins

        real_import = builtins.__import__

        def no_vco_lib(name, *args, **kwargs):
            if name == "vco_lib" or name.startswith("vco_lib."):
                raise ImportError("No module named 'vco_lib' (test)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_vco_lib)
        run = _ScriptedRun(monkeypatch, [QUOTA_FAILURE, QUOTA_FAILURE])
        for _ in range(2):
            with pytest.raises(sb.BackendUnavailable):
                sb.call_cli("node")
        err = capsys.readouterr().err
        assert err.count("vco_lib is not importable") == 1
        assert "API-key retry is unavailable" in err
        assert len(run.calls) == 2  # no retry spawned
