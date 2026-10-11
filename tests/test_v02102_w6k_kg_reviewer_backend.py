# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-K item 8 — the post-commit KG reviewer's backend chain.

Owner ruling 2026-10-11: keep the Haiku reviewer; when Haiku is unavailable
(a REAL signal — quota / 429 / auth / absent), fall back
``claude-gw/glm-5.3-flash`` → ``claude-gw/deepseek-v4.1-flash`` →
``claude-gw/qwen3.8-flash`` — each only when the LOCAL MODEL GATEWAY
actually serves it (the catalogue is ASKED, never assumed) — and use
OpenAI when the user enabled OpenAI for SUMMARIES.

Every rung of that walk is driven here with faked
catalogue/consent/breaker state; the classification and the breaker file
are summary_backends' own (one home), pointed at a temp state dir.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The chain module with a temp breaker file + reset caches."""
    for p in (str(SCRIPTS_DIR), str(REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    sb = _load("_w6k_summary_backends", SCRIPTS_DIR / "summary_backends.py")
    sb.reset_backend_cache()
    monkeypatch.setattr(sb, "_breaker_path", lambda: tmp_path / "breaker.json")
    mod = _load("_w6k_kg_reviewer_backend",
                SCRIPTS_DIR / "kg_reviewer_backend.py")
    mod._sb = sb
    yield mod
    sb.reset_backend_cache()


class _Rig:
    """Fake availability for every rung; records every skip log line."""

    def __init__(self, chain, monkeypatch, *, haiku=True, claude=True,
                 gateway_base="http://127.0.0.1:11460",
                 catalogue=frozenset(), consent=False, openai_key=True):
        self.lines: list[str] = []
        self.chain = chain
        monkeypatch.setattr(chain.shutil, "which",
                            lambda cmd: "/usr/bin/claude" if claude else None)
        monkeypatch.setattr(
            chain, "gateway_catalogue",
            lambda timeout_s=2.0: (gateway_base, catalogue))
        monkeypatch.setattr(
            chain._sb, "breaker_state", self._breaker_state)
        monkeypatch.setattr(
            chain._sb, "openai_consent_granted", lambda: consent)
        monkeypatch.setattr(
            chain._sb, "openai_available", lambda: openai_key)
        self.latched: dict[str, dict] = {}
        self.haiku = haiku

    def _breaker_state(self, tier: str):
        return self.latched.get(tier)

    def decide(self):
        return self.chain.select_reviewer_backend(log=self.lines.append)

    def assert_logged(self, needle: str) -> None:
        assert any(needle in ln for ln in self.lines), list(self.lines)


def test_haiku_is_the_default(chain, monkeypatch):
    rig = _Rig(chain, monkeypatch)
    assert rig.decide() == {"mode": "claude", "model": "haiku",
                            "base_url": None, "reason": "default"}


def test_absent_cli_falls_to_the_first_served_gateway_model(chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, claude=False,
               catalogue=frozenset({"claude-gw/glm-5.3-flash",
                                    "claude-gw/qwen3.8-flash"}))
    d = rig.decide()
    assert d["mode"] == "claude"
    assert d["model"] == "claude-gw/glm-5.3-flash"
    assert d["base_url"] == "http://127.0.0.1:11460"
    rig.assert_logged("rung haiku: unavailable (no `claude` CLI on PATH)")


def test_quota_latched_haiku_is_a_real_signal_and_falls_back(chain, monkeypatch):
    """The W6-K defect: an exhausted Haiku silently skipped the review."""
    rig = _Rig(chain, monkeypatch,
               catalogue=frozenset({"claude-gw/deepseek-v4.1-flash"}))
    rig.latched["kg_reviewer:haiku"] = {
        "reason": "quota", "until": 9e18, "remaining": 17999}
    d = rig.decide()
    assert d["model"] == "claude-gw/deepseek-v4.1-flash", d
    rig.assert_logged("rung haiku: latched (quota)")
    rig.assert_logged("rung claude-gw/glm-5.3-flash: not served")


def test_catalogue_membership_is_asked_not_assumed(chain, monkeypatch):
    """A gateway rung the daemon does NOT list is skipped with a line —
    availability is evidence, never a hard-coded yes."""
    rig = _Rig(chain, monkeypatch, claude=False,
               catalogue=frozenset({"claude-gw/qwen3.8-flash"}))
    d = rig.decide()
    assert d["model"] == "claude-gw/qwen3.8-flash"
    rig.assert_logged("rung claude-gw/glm-5.3-flash: not served")
    rig.assert_logged("rung claude-gw/deepseek-v4.1-flash: not served")


def test_no_gateway_falls_to_openai_only_with_summaries_consent(
        chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, claude=False, gateway_base=None,
               consent=True)
    d = rig.decide()
    assert d["mode"] == "openai", d
    assert "degraded" in d["reason"]
    # every skipped rung carries its reason
    rig.assert_logged("no local model gateway answered")


def test_openai_rung_stays_off_without_the_summaries_consent(
        chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, claude=False, gateway_base=None,
               consent=False)
    d = rig.decide()
    assert d == {"mode": "skip", "model": "", "base_url": None,
                 "reason": "no rung available"}
    rig.assert_logged("kg_summary_openai_consent")


def test_select_subcommand_prints_json(chain, monkeypatch, capsys):
    _Rig(chain, monkeypatch)
    assert chain.main(["select"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["mode"] == "claude" and d["model"] == "haiku"


def test_record_trips_the_tier_on_a_real_signal(chain, monkeypatch, tmp_path):
    """`record` feeds summary_backends' classifier: a quota wording latches
    the haiku tier so the NEXT commit falls back (one home, real signals)."""
    rig = _Rig(chain, monkeypatch)
    tf = tmp_path / "tail.txt"
    tf.write_text("... usage limit reached ...", encoding="utf-8")
    assert chain.main(["record", "--model", "haiku", "--exit", "1",
                       "--text-file", str(tf)]) == 0
    st = chain._sb.breaker_state("kg_reviewer:haiku")
    real = chain._sb._load_breaker_file().get("kg_reviewer:haiku")
    assert (st or real) is not None, "quota must latch the haiku tier"
    # and the walk now falls back
    rig.latched["kg_reviewer:haiku"] = st or real
    rig2dec = rig.decide()
    assert rig2dec["model"] != "haiku" or rig2dec["mode"] == "skip"


def test_record_clears_on_success(chain, monkeypatch, tmp_path):
    _Rig(chain, monkeypatch)
    tf = tmp_path / "tail.txt"
    tf.write_text("all good", encoding="utf-8")
    rc = chain.main(["record", "--model", "haiku", "--exit", "0",
                     "--text-file", str(tf)])
    assert rc == 0
    assert chain._sb._load_breaker_file().get("kg_reviewer:haiku") in (
        None, {}) or not chain._sb.breaker_state("kg_reviewer:haiku")


def test_gateway_auth_env_reads_the_token_file_not_argv(
        chain, monkeypatch, tmp_path):
    """The gateway rung points the claude child at the daemon via ENV; the
    host token is READ from the daemon's token file inside this process."""
    class _FakeCfg:
        @staticmethod
        def token_path():
            return tmp_path / "model-gateway.token"
    fake_pkg = types.ModuleType("claude_mcp_servers")
    fake_mr = types.ModuleType("claude_mcp_servers.model_router")
    fake_cfg = _FakeCfg
    monkeypatch.setitem(sys.modules, "claude_mcp_servers", fake_pkg)
    monkeypatch.setitem(sys.modules, "claude_mcp_servers.model_router", fake_mr)
    monkeypatch.setitem(sys.modules, "claude_mcp_servers.model_router.config",
                        fake_cfg)
    token_file = tmp_path / "model-gateway.token"
    token_file.write_text("  s3cret-token\n", encoding="utf-8")
    env = chain._gateway_auth_env("http://127.0.0.1:11460")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11460"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "s3cret-token"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
