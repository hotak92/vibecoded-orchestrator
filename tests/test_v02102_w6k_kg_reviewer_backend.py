# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-K item 8 — the post-commit KG reviewer's backend chain.

Owner ruling 2026-10-11, revised 2026-10-15 (v0.2.103): the reviewer runs
the CHOSEN Claude model (one machine-wide setting; these tests pin it to
``haiku`` through ``VCO_KG_REVIEW_MODEL`` so the chain mechanics stay the
subject); when it is unavailable (a REAL signal — quota / 429 / auth /
absent), fall back to the comparable non-flash gateway rungs
``claude-gw/glm-5.3[1m]`` → ``claude-gw/kimi/k3[1m]`` — each only when the
LOCAL MODEL GATEWAY actually serves it (the catalogue is ASKED, never
assumed) — and use OpenAI when the user enabled OpenAI for SUMMARIES. The
retired flash rungs are covered in tests/test_v02103_kg_reviewer_safety.py.

Every rung of that walk is driven here with faked
catalogue/consent/breaker state; the classification and the breaker file
are summary_backends' own (one home), pointed at a temp state dir.
"""
from __future__ import annotations

import http.server
import importlib
import importlib.util
import json
import subprocess as _subprocess
import sys
import threading
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
                 catalogue=frozenset(), consent=False, openai_key=True,
                 refusal=None):
        self.lines: list[str] = []
        self.chain = chain
        monkeypatch.setenv("VCO_KG_REVIEW_MODEL", "haiku")
        monkeypatch.setattr(chain.shutil, "which",
                            lambda cmd: "/usr/bin/claude" if claude else None)
        monkeypatch.setattr(
            chain, "gateway_catalogue",
            lambda timeout_s=2.0: (gateway_base, catalogue, refusal))
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


def test_the_configured_model_is_the_first_rung(chain, monkeypatch):
    rig = _Rig(chain, monkeypatch)
    assert rig.decide() == {"mode": "claude", "model": "haiku",
                            "base_url": None, "reason": "chosen model (env)"}


def test_latched_model_falls_to_the_first_served_gateway_model(
        chain, monkeypatch):
    # W6-N F-2: an ABSENT CLI now skips the gateway rungs too (they spawn
    # `claude -p`) — see test_v02102_w6n_kg_reviewer_fixes.py.
    rig = _Rig(chain, monkeypatch,
               catalogue=frozenset({"claude-gw/glm-5.3[1m]",
                                    "claude-gw/kimi/k3[1m]"}))
    rig.latched["kg_reviewer:haiku"] = {"reason": "quota", "remaining": 1}
    d = rig.decide()
    assert d["mode"] == "claude"
    assert d["model"] == "claude-gw/glm-5.3[1m]"
    assert d["base_url"] == "http://127.0.0.1:11460"
    rig.assert_logged("rung haiku: latched (quota)")


def test_quota_latched_model_is_a_real_signal_and_falls_back(chain, monkeypatch):
    """The W6-K defect: an exhausted model silently skipped the review."""
    rig = _Rig(chain, monkeypatch,
               catalogue=frozenset({"claude-gw/kimi/k3[1m]"}))
    rig.latched["kg_reviewer:haiku"] = {
        "reason": "quota", "until": 9e18, "remaining": 17999}
    d = rig.decide()
    assert d["model"] == "claude-gw/kimi/k3[1m]", d
    rig.assert_logged("rung haiku: latched (quota)")
    rig.assert_logged("rung claude-gw/glm-5.3: not served")


def test_catalogue_membership_is_asked_not_assumed(chain, monkeypatch):
    """A gateway rung the daemon does NOT list is skipped with a line —
    availability is evidence, never a hard-coded yes. A served FLASH model
    is never a rung (v0.2.103)."""
    rig = _Rig(chain, monkeypatch,
               catalogue=frozenset({"claude-gw/glm-5.3-flash[1m]",
                                    "claude-gw/kimi/k3[1m]"}))
    rig.latched["kg_reviewer:haiku"] = {"reason": "quota", "remaining": 1}
    d = rig.decide()
    assert d["model"] == "claude-gw/kimi/k3[1m]"
    rig.assert_logged("rung claude-gw/glm-5.3: not served")


def test_no_gateway_falls_to_openai_only_with_summaries_consent(
        chain, monkeypatch):
    rig = _Rig(chain, monkeypatch, gateway_base=None, consent=True)
    rig.latched["kg_reviewer:haiku"] = {"reason": "quota", "remaining": 1}
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


# ═══════════════════════════════════════════════════════════════════════
# Review fixes 2026-10-11 (W6-K review F1/F2/F4, MERGE-AFTER-FIXES round)
# ═══════════════════════════════════════════════════════════════════════



class _AuthRequiredCatalogue(http.server.BaseHTTPRequestHandler):
    """A stub gateway ``/v1/models`` that REQUIRES the host bearer token —
    the shape of the REAL daemon (model_router's server 401s a missing
    token on every route). Drives the REAL urllib request inside
    ``gateway_catalogue`` — the probe is never monkeypatched here."""

    token = "stub-host-token"

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        auth = self.headers.get("Authorization") or ""
        if auth != f"Bearer {self.token}":
            self._reply(401, {"type": "error",
                              "error": {"type": "authentication_error",
                                        "message": "missing host token"}})
            return
        # The live shape (W6-N F-1): ``[1m]`` rows, ``qwen/`` namespace.
        self._reply(200, {"data": [{"id": "claude-gw/glm-5.3-flash[1m]"},
                                   {"id": "claude-gw/qwen/qwen3.8-flash[1m]"}]})

    def _reply(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence the test log
        pass


@pytest.fixture()
def stub_gateway():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _AuthRequiredCatalogue)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _point_at_stub(monkeypatch, tmp_path, port, token_text):
    """Route the chain's port resolution and token read at the stub."""
    import vco_lib.vscode_settings as _vs
    import claude_mcp_servers.model_router.config as _mrc
    monkeypatch.setattr(_vs, "resolve_gateway_ports", lambda: (port,))
    token_file = tmp_path / "model-gateway.token"
    if token_text is not None:
        token_file.write_text(token_text, encoding="utf-8")
    monkeypatch.setattr(_mrc, "token_path", lambda: token_file)


def test_catalogue_probe_authenticates_with_the_host_token(
        chain, monkeypatch, tmp_path, stub_gateway):
    """F1 (HIGH): the real daemon 401s a tokenless ``/v1/models``, so a
    probe without ``Authorization: Bearer <host token>`` makes EVERY
    gateway rung dead in production. The token comes from the same token
    file ``_gateway_auth_env`` reads (one helper, one home)."""
    _point_at_stub(monkeypatch, tmp_path, stub_gateway,
                   "  stub-host-token\n")
    res = chain.gateway_catalogue()
    assert res[0] == f"http://127.0.0.1:{stub_gateway}", res
    ids = res[1]
    assert "claude-gw/glm-5.3-flash[1m]" in ids, ids
    assert "claude-gw/qwen/qwen3.8-flash[1m]" in ids, ids


def test_a_refused_probe_is_distinguished_from_no_gateway(
        chain, monkeypatch, tmp_path, stub_gateway):
    """F1: the gateway ANSWERING with 401 (no readable token file here) is
    not "no local model gateway answered" — the third return element
    carries the refusal so the skip log line can stay honest."""
    _point_at_stub(monkeypatch, tmp_path, stub_gateway, None)
    res = chain.gateway_catalogue()
    assert res[0] is None, res
    assert not res[1], res
    refusal = res[2]
    assert refusal and "401" in refusal, res


def test_a_refused_gateway_is_logged_as_a_refusal(chain, monkeypatch):
    """F1: the skip line for a 401'd probe names the refusal — the old
    wording ("no local model gateway answered") was FALSE for a 401."""
    rig = _Rig(chain, monkeypatch, gateway_base=None,
               refusal="HTTP 401 from http://127.0.0.1:11460")
    rig.latched["kg_reviewer:haiku"] = {"reason": "quota", "remaining": 1}
    rig.decide()
    rig.assert_logged("refused the catalogue probe")
    rig.assert_logged("HTTP 401")
    assert not any("no local model gateway answered" in ln
                   for ln in rig.lines), rig.lines


# F2 (the leaked mktemp prompt file) is superseded in v0.2.103: no prompt
# file exists any more — `run` builds the prompt itself
# (vco_lib.kg_reviewer_prompt), so nothing is staged to leak.


def test_run_handles_a_hung_reviewer_timeout(chain, monkeypatch, tmp_path):
    """F4 (LOW): a ``TimeoutExpired`` from the claude child must be HANDLED
    — classified, logged, non-zero exit — not an uncaught traceback dumped
    into the detached log."""
    _Rig(chain, monkeypatch)  # haiku rung available
    repo = tmp_path / "repo"
    repo.mkdir()
    real_run = _subprocess.run
    for args in (["init", "-q"], ["-c", "user.name=t", "-c",
                                  "user.email=t@e.invalid", "commit", "-q",
                                  "--allow-empty", "-m", "c"]):
        real_run(["git", *args], cwd=repo, check=True, capture_output=True)

    def _raise(cmd, *a, **kw):
        if cmd and cmd[0] == "claude":
            raise _subprocess.TimeoutExpired(cmd=["claude"], timeout=900)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(chain, "subprocess", types.SimpleNamespace(
        run=_raise, TimeoutExpired=_subprocess.TimeoutExpired))
    log = tmp_path / "review.log"
    rc = chain.main(["run", "--project-root", str(repo), "--log", str(log)])
    assert rc == 124, rc
    assert "TIMED OUT" in log.read_text(encoding="utf-8")


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
