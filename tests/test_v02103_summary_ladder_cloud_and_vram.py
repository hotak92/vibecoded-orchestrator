# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the summary ladder's cloud tiers, the local-tier VRAM guard,
and the CLI isolation-flag probe (owner fixes 8b / 8c / 8g, 2026-10-14).

8b — cloud providers before local (order: claude CLI → z.ai GLM → QwenCloud
→ local Ollama). A cloud tier is used only when that vendor's key resolves
through the model gateway's OWN registry + resolver; no key → the tier is
skipped silently, never an error. A model REFUSAL stays FINAL (no tier
descent); only tier-level failures demote via the existing breaker.

8c — the local tier must not compete with the code-embedding re-index for
VRAM: a code-graph walk (launcher ledger row pending/running, or the
code-embed service's /health in-flight fields) or free VRAM below the floor
defers the node (owed work) instead of loading the model. VRAM probing
soft-fails to unknown → proceed only when no walk is active.

8g — ``cli_isolation_args`` wrongly decided the installed claude CLI lacks
``--strict-mcp-config``: the CLI's own help writer intermittently truncates
at an ~8 KiB pipe boundary (measured 8190/8191/16381 bytes where the full
text is 22 336), and on CLI 2.1.285 the flag's only documentation sits
~20 KB in, inside another option's prose. The fix re-reads until stable.

All synthetic: no LLM call, no vendor endpoint, no Weaviate, no keychain.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import types
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SB_PATH = _REPO_ROOT / "templates" / "scripts" / "summary_backends.py"
_HELP_FIXTURE = (_REPO_ROOT / "tests" / "fixtures" / "summary_ladder"
                 / "claude-cli-help-2.1.285.txt")

_SUMMARY_ENV = (
    "KG_SUMMARY_BACKEND", "CODE_SUMMARY_BACKEND", "KG_SUMMARY_TIMEOUT",
    "VCO_SUMMARY_BREAKER", "KG_SUMMARY_MIN_FREE_VRAM_MB",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
)


def _load(name: str, path: Path = _SB_PATH) -> types.ModuleType:
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Fresh summary_backends: isolated state dir, no env leakage, captured
    log lines, no real tier probe."""
    target = tmp_path / "vct-state"
    target.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(target))
    for key in _SUMMARY_ENV:
        monkeypatch.delenv(key, raising=False)
    mod = _load("_v02103_sb")
    mod.reset_backend_cache()
    mod.reset_breaker(persisted=True)
    lines: list[str] = []
    mod.set_logger(lines.append)
    setattr(mod, "log_lines", lines)
    # Keep the REAL local-tier probe reachable: the VRAM tests exercise it
    # while every tier probe is pinned off (see _pin_tiers).
    setattr(mod, "real_ollama_available", mod.ollama_available)
    # Every probe pinned by default: a test that wants a tier enables it.
    _pin_tiers(monkeypatch, mod)
    return mod


def _pin_tiers(monkeypatch: pytest.MonkeyPatch, mod, *, cli=False, zai=False,
               qwen=False, ollama=False, openai=False, api=False) -> None:
    monkeypatch.setattr(mod, "cli_available", lambda: cli)
    monkeypatch.setattr(mod, "zai_available", lambda: zai)
    monkeypatch.setattr(mod, "qwen_available", lambda: qwen)
    monkeypatch.setattr(mod, "ollama_available", lambda: ollama)
    monkeypatch.setattr(mod, "openai_available", lambda: openai)
    monkeypatch.setattr(mod, "api_available", lambda: api)


# ─── 8b: tier order ────────────────────────────────────────────────────────

@pytest.mark.parametrize("tiers,expected", [
    ({"cli": True, "zai": True, "qwen": True, "ollama": True}, "cli"),
    ({"cli": False, "zai": True, "qwen": True, "ollama": True}, "zai"),
    ({"cli": False, "zai": False, "qwen": True, "ollama": True}, "qwen"),
    ({"cli": False, "zai": False, "qwen": False, "ollama": True}, "ollama"),
    ({"cli": False, "zai": False, "qwen": False, "ollama": False,
      "openai": False, "api": True}, "api"),
])
def test_cloud_tiers_sit_between_cli_and_local(sb, monkeypatch, tiers,
                                               expected):
    """Owner order 2026-10-14: cli → z.ai → QwenCloud → Ollama → (unchanged)."""
    _pin_tiers(monkeypatch, sb, **tiers)
    assert sb.select_backend() == expected, tiers


def test_valid_backends_accept_the_new_tier_names(sb, monkeypatch):
    assert {"zai", "qwen"} <= sb.VALID_BACKENDS
    monkeypatch.setenv("KG_SUMMARY_BACKEND", "qwen")
    assert sb.select_backend() == "qwen"


# ─── 8b: key resolution through the ONE registry ───────────────────────────

class _Row:
    """A Vendor-shaped stand-in: only the fields _vendor_tier_state reads."""
    def __init__(self, vendor_id, *, upstream="https://v.example/api",
                 auth_header="Authorization", auth_scheme="Bearer ",
                 short_name="Example", key="k-SECRET-VALUE"):
        self.vendor_id = vendor_id
        self.upstream = upstream
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.short_name = short_name
        self._key = key


class _Rung:
    def __init__(self, to_vendor_id, to_model_id):
        self.to_vendor_id = to_vendor_id
        self.to_model_id = to_model_id


class _Resolver:
    def __init__(self, key):
        self._key = key

    class _R:
        def __init__(self, key):
            self.key = key

    def resolve(self, vendor):
        return self._R(self._key)


def _fake_registry(monkeypatch, sb, *, zai_key="", qwen_key=""):
    """A synthetic gateway registry: zai + qwen rows, the gateway's own
    fallback-ladder models, per-vendor keys."""
    rows = {
        "zai": _Row("zai", short_name="GLM", key=zai_key),
        "qwen": _Row("qwen", upstream="https://q.example/anthropic",
                     short_name="Qwen", key=qwen_key),
    }
    ladder = (_Rung("zai", "glm-5.3-flash"), _Rung("qwen", "qwen3.8-flash"))

    class _Dispatch:
        def resolve(self, vendor):
            return _Resolver(vendor._key).resolve(vendor)

    def _load():
        return rows, ladder, "2023-06-01", _Dispatch()

    monkeypatch.setattr(sb, "_load_gateway_registry", _load)


def test_vendor_state_model_is_the_gateway_cheap_rung(sb, monkeypatch):
    """The summary model comes from the gateway's own FALLBACK_LADDER —
    glm-5.3-flash / qwen3.8-flash, the cheap escalation rungs (summaries
    are short). No vendor model id is duplicated here."""
    _fake_registry(monkeypatch, sb, zai_key="z", qwen_key="q")
    assert sb._vendor_summary_model("zai") == "glm-5.3-flash"
    assert sb._vendor_summary_model("qwen") == "qwen3.8-flash"
    assert sb._vendor_available("zai") and sb._vendor_available("qwen")


def test_no_key_skips_the_tier_silently(sb, monkeypatch):
    """No key → tier not selectable, ONE debug line, never an error —
    a third-party user without the provider never sees a failure."""
    _fake_registry(monkeypatch, sb, zai_key="", qwen_key="")
    assert sb._vendor_available("zai") is False
    assert sb._vendor_available("qwen") is False
    zai_lines = [ln for ln in sb.log_lines if "'zai'" in ln]
    assert len(zai_lines) == 1, sb.log_lines
    assert "not selectable" in zai_lines[0]


def test_gateway_package_absent_skips_the_tiers(sb, monkeypatch):
    def _boom():
        raise ImportError("no model_router on this PYTHONPATH")

    monkeypatch.setattr(sb, "_load_gateway_registry", _boom)
    assert sb._vendor_available("zai") is False
    assert sb.select_backend() == "skip"          # no error raised
    assert any("model gateway package unavailable" in ln
               for ln in sb.log_lines)


def test_key_value_never_reaches_a_log_line(sb, monkeypatch):
    _fake_registry(monkeypatch, sb, zai_key="k-SUPER-SECRET-ZAI")
    sb.zai_available()
    assert not any("k-SUPER-SECRET-ZAI" in ln for ln in sb.log_lines)


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_call_vendor_builds_the_registered_request_shape(sb, monkeypatch):
    """URL, auth header and version header come from the registry row;
    the payload is the Anthropic /v1/messages shape the api tier uses."""
    _fake_registry(monkeypatch, sb, zai_key="k-ZAI")
    seen: dict = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["data"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"content": [
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": " Returns the resolved collection name. "},
        ]})

    monkeypatch.setattr(sb.urllib.request, "urlopen", _fake_urlopen)
    out = sb.call_zai("summarise: foo()")
    assert out == "Returns the resolved collection name."
    assert seen["url"] == "https://v.example/api/v1/messages"
    headers = {k.lower(): v for k, v in seen["headers"].items()}
    assert headers["authorization"] == "Bearer k-ZAI"
    assert headers["anthropic-version"] == "2023-06-01"
    assert seen["data"]["model"] == "glm-5.3-flash"
    assert seen["data"]["system"] == sb.SYSTEM_PROMPT
    assert seen["data"]["messages"] == [{"role": "user",
                                         "content": "summarise: foo()"}]


# ─── 8b: refusal final, tier failure demotes ───────────────────────────────

def test_a_refusal_is_final_no_tier_descent(sb, monkeypatch):
    """Owner ruling: is_non_answer → NonAnswerResponse, the ladder never
    answers a content condition with a weaker model."""
    _pin_tiers(monkeypatch, sb, cli=True, zai=True, ollama=True)
    monkeypatch.setattr(sb, "call_cli", lambda p: "I cannot do that.")
    called: list[str] = []
    monkeypatch.setattr(sb, "call_zai",
                        lambda p: called.append("zai") or "x" * 40)
    monkeypatch.setattr(sb, "call_ollama",
                        lambda p, **kw: called.append("ollama") or "y" * 40)
    with pytest.raises(sb.NonAnswerResponse):
        sb.call_llm("prompt")
    assert called == [], "a refusal must not descend the ladder"
    assert sb.breaker_state("cli") is None, "content is not a tier failure"


def test_tier_failure_demotes_to_the_next_cloud_tier(sb, monkeypatch):
    _pin_tiers(monkeypatch, sb, zai=True, qwen=True)
    sb._BACKEND_CACHE["choice"] = "zai"

    def _boom(prompt):
        raise urllib.error.HTTPError(
            "https://v.example/api/v1/messages", 429,
            "Too Many Requests", Message(), None)

    monkeypatch.setattr(sb, "call_zai", _boom)
    monkeypatch.setattr(sb, "call_qwen",
                        lambda p: "Returns the resolved collection name.")
    assert sb.call_llm("x") == "Returns the resolved collection name."
    assert sb.breaker_state("zai")["reason"] == "rate_limit"
    assert sb.breaker_state("qwen") is None


def test_qwen_is_served_by_the_shared_dispatch(sb, monkeypatch):
    """The dispatch arm routes the tier id to the vendor call — one ladder,
    no side door (QwenCloud serves ONLY this fallback)."""
    monkeypatch.setattr(sb, "call_qwen", lambda p: "Returns per-row filters.")
    assert sb._dispatch("qwen", "p") == "Returns per-row filters."


# ─── 8c: the local tier's VRAM / walk guard ────────────────────────────────

def _tags_ok(sb, monkeypatch):
    """Ollama answers /api/tags and holds the model (the pre-8c checks),
    and the REAL ollama_available runs (the fixture pins it off)."""
    monkeypatch.setattr(sb, "ollama_available", sb.real_ollama_available)
    monkeypatch.setattr(sb, "OLLAMA_DEFAULT_MODEL", "qwen3.5:9b",
                        raising=False)

    def _fake_urlopen(url, timeout=None):
        if str(url).endswith("/api/tags"):
            return _FakeResponse({"models": [{"name": "qwen3.5:9b"}]})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(sb.urllib.request, "urlopen", _fake_urlopen)


def test_active_walk_defers_the_local_tier(sb, monkeypatch):
    _tags_ok(sb, monkeypatch)
    monkeypatch.setattr(sb, "_codegraph_walk_active", lambda: True)
    monkeypatch.setattr(sb, "_free_vram_mb", lambda: None)
    assert sb.ollama_available() is False
    assert any("deferred" in ln and "walk" in ln for ln in sb.log_lines), \
        sb.log_lines


def test_idle_and_vram_free_uses_the_local_tier(sb, monkeypatch):
    _tags_ok(sb, monkeypatch)
    monkeypatch.setattr(sb, "_codegraph_walk_active", lambda: False)
    monkeypatch.setattr(sb, "_free_vram_mb", lambda: 20000)
    assert sb.ollama_available() is True


def test_low_vram_defers_the_local_tier(sb, monkeypatch):
    _tags_ok(sb, monkeypatch)
    monkeypatch.setattr(sb, "_codegraph_walk_active", lambda: False)
    monkeypatch.setattr(sb, "_free_vram_mb", lambda: 100)
    assert sb.ollama_available() is False
    assert any("VRAM" in ln for ln in sb.log_lines)


def test_the_vram_floor_is_an_env_knob(sb, monkeypatch):
    _tags_ok(sb, monkeypatch)
    monkeypatch.setattr(sb, "_codegraph_walk_active", lambda: False)
    monkeypatch.setattr(sb, "_free_vram_mb", lambda: 100)
    monkeypatch.setenv("KG_SUMMARY_MIN_FREE_VRAM_MB", "50")
    assert sb.ollama_available() is True


def test_unknown_vram_proceeds_when_no_walk_is_active(sb, monkeypatch):
    _tags_ok(sb, monkeypatch)
    monkeypatch.setattr(sb, "_codegraph_walk_active", lambda: False)
    monkeypatch.setattr(sb, "_free_vram_mb", lambda: None)
    assert sb.ollama_available() is True


def test_deferred_local_descends_to_the_cloud_tier(sb, monkeypatch):
    """The point of 8b+8c together: with a vendor key on the machine, the
    cloud tier serves even while a walk runs — selection reaches the local
    tier only when no cloud tier answered, so the GPU is left to the
    re-index."""
    _pin_tiers(monkeypatch, sb, zai=True, ollama=True)
    monkeypatch.setattr(sb, "_codegraph_walk_active", lambda: True)
    assert sb.select_backend() == "zai"


def _make_builds_db(state_dir: Path, statuses: list[str]) -> None:
    db = state_dir / "launcher.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS code_graph_builds "
                 "(id TEXT, project_id TEXT, status TEXT)")
    conn.execute("DELETE FROM code_graph_builds")
    for i, status in enumerate(statuses):
        conn.execute("INSERT INTO code_graph_builds VALUES (?, 'p', ?)",
                     (f"b{i}", status))
    conn.commit()
    conn.close()


def test_walk_signal_reads_the_launcher_ledger(sb, tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_EMBED_SERVICE_URL", "http://127.0.0.1:9")
    state = tmp_path / "vct-state"
    _make_builds_db(state, ["success", "running"])
    monkeypatch.setattr(sb, "_vct_state_dir", lambda: state)
    assert sb._codegraph_walk_active() is True
    _make_builds_db(state, ["success", "failed", "partial", "skipped"])
    assert sb._codegraph_walk_active() is False


def test_walk_signal_reads_the_embed_service_health(sb, monkeypatch):
    def _fake_urlopen(url, timeout=None):
        assert str(url) == "http://127.0.0.1:9/health"
        return _FakeResponse({"in_flight": 2, "inference_busy": False})

    monkeypatch.setattr(sb.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(sb, "_vct_state_dir",
                        lambda: Path("/nonexistent-no-db"))
    assert sb._codegraph_walk_active() is True


def test_walk_signal_soft_fails_to_not_active(sb, monkeypatch):
    """Unreachable service + no DB: a failed probe is not evidence."""
    monkeypatch.setenv("CODE_EMBED_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(sb, "_vct_state_dir",
                        lambda: Path("/nonexistent-no-db"))
    assert sb._codegraph_walk_active() is False


def test_free_vram_probe_absent_smi_is_unknown(sb, monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    assert sb._free_vram_mb() is None


def test_free_vram_probe_parses_nvidia_smi(sb, monkeypatch):
    class _R:
        returncode = 0
        stdout = "5327\n12000\n"

    def _fake_run(argv, **kwargs):
        assert "nvidia-smi" in argv[0]
        assert any("memory.free" in a for a in argv)
        return _R()

    monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert sb._free_vram_mb() == 5327


# ─── 8c: the shared num_ctx is sized to the prompts, not the old 32k ───────

def test_generate_num_ctx_is_the_smallest_shared_window():
    """v0.2.103 8c: ONE shared value is required (a per-caller window
    reloads the model), so it is the smallest that fits every generation
    prompt VCO sends — the largest Ollama-bound prompt is a 4 000-char
    body (~1.3k tokens; batched prompts go to the CLI tier) + system +
    the 1 024 num_predict budget ≈ 3k tokens. 32 768 loaded a KV cache 4×
    larger than any prompt needs (the field GPU OOM, 2026-10-14)."""
    from vco_lib import ollama_generate

    assert ollama_generate.GENERATE_NUM_CTX_DEFAULT == 8192
    assert ollama_generate.generate_num_ctx() == 8192


# ─── 8g: the CLI isolation-flag probe on real help output ──────────────────

@pytest.fixture(scope="module")
def real_help() -> str:
    """The RECORDED full ``claude --help`` output of CLI 2.1.285 (this
    machine, 2026-10-14) — 22 336 bytes. ``--strict-mcp-config`` appears
    only inside another option's description prose, ~20 KB in."""
    return _HELP_FIXTURE.read_text(encoding="utf-8")


def test_recorded_full_help_lists_the_isolation_flags(sb, real_help):
    assert sb._cli_lists_flag(real_help, "--strict-mcp-config")
    assert sb._cli_lists_flag(real_help, "--mcp-config")
    assert sb._cli_lists_flag(real_help, "--settings")
    assert not sb._cli_lists_flag(real_help, "--no-such-flag")


def test_truncated_help_prefix_lacks_the_flag(sb, real_help):
    """The measured truncation shape: a PREFIX cut at the ~8 KiB pipe
    boundary — exactly what made the probe misclassify before the fix."""
    truncated = real_help[:8190]
    assert len(truncated) < len(real_help)
    assert not sb._cli_lists_flag(truncated, "--strict-mcp-config")


class _FakeProc:
    def __init__(self, stdout: str):
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _fake_help_reads(monkeypatch, outputs: list[str]) -> None:
    """Serve the given stdout strings one per ``--help`` read."""
    served = list(outputs)

    def _fake_run(argv, **kwargs):
        assert "--help" in argv
        if not served:
            raise AssertionError("more --help reads than the test scripted")
        return _FakeProc(served.pop(0))

    monkeypatch.setattr(subprocess, "run", _fake_run)


def test_cli_help_text_recovers_the_full_text_after_a_truncated_read(
        sb, monkeypatch, real_help):
    """8g's measured shape: an intermittent ~8 KiB truncation. Two stable
    consecutive reads certify completeness; before the fix, the FIRST
    (truncated) read was returned and every headless call started the
    user's MCP servers."""
    _fake_help_reads(
        monkeypatch,
        [real_help[:8191], real_help, real_help, real_help],
    )
    assert sb._cli_help_text("/usr/bin/claude") == real_help + "\n"


def test_isolation_args_added_on_the_recorded_help(sb, monkeypatch,
                                                   real_help):
    _fake_help_reads(monkeypatch, [real_help, real_help])
    args = sb.cli_isolation_args("/usr/bin/claude")
    assert "--strict-mcp-config" in args
    assert "--mcp-config" in args
    assert "--settings" in args
    assert not any("does not offer" in ln for ln in sb.log_lines), \
        sb.log_lines


def test_isolation_args_still_log_once_when_help_really_lacks_flags(
        sb, monkeypatch):
    _fake_help_reads(monkeypatch, ["usage: claude [-h]\n\nOptions:\n  -h\n",
                                   "usage: claude [-h]\n\nOptions:\n  -h\n"])
    args = sb.cli_isolation_args("/usr/bin/claude")
    assert args == []
    assert any("does not offer" in ln for ln in sb.log_lines), sb.log_lines
