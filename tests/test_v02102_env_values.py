# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""v0.2.102 W6-FIN — ``vco_lib.env_values.env_int``, the one env-int reader.

Three layers:

1. the helper's own contract (unset / empty / garbage / bounds / clamp /
   mapping / ``None`` default);
2. CALLER MATRIX — every migrated caller keeps the exact behaviour its private
   reader had on empty, garbage, zero and negative values. These tests also
   pass against the pre-migration private readers: that is the point (the
   migration changes where the rule lives, not what any caller does);
3. PARITY — the stdlib-only scripts that cannot import ``vco_lib`` keep a
   documented copy; each copy is pinned to ``env_int`` with the same bounds.

Plus the env-keys ratchet: keys read only through ``env_int`` are still seen
by ``tests/test_v0292_env_keys_documented.py``'s scanner.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "templates" / "scripts"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))

from vco_lib.env_values import env_int  # noqa: E402

K = "VCO_W6FIN_PROBE_INT"

#: Raw values every reader is probed with (None = unset).
PROBES = [None, "", "   ", "abc", "1.5", "-3", "0", "1", "7", " 7 ", "65535", "65536"]


def _expected_positive(raw, default):
    try:
        v = int((raw or "").strip())
    except ValueError:
        return default
    return v if v > 0 else default


def _expected_any(raw, default):
    try:
        return int((raw or "").strip())
    except ValueError:
        return default


# ── 1. the helper's contract ──────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    (None, 5), ("", 5), ("   ", 5), ("abc", 5), ("1.5", 5), ("0x10", 5),
    ("-3", -3), ("0", 0), ("7", 7), (" 7 ", 7), ("1_000", 1000),
])
def test_env_int_unbounded(raw, expected):
    env = {} if raw is None else {K: raw}
    assert env_int(K, 5, env=env) == expected


@pytest.mark.parametrize("raw,expected", [("-3", 5), ("0", 5), ("1", 1), ("9", 9)])
def test_env_int_minimum_falls_back_to_default(raw, expected):
    assert env_int(K, 5, env={K: raw}, minimum=1) == expected


@pytest.mark.parametrize("raw,expected", [("-3", 60), ("59", 60), ("60", 60), ("1800", 1800),
                                          ("x", 900), ("", 900)])
def test_env_int_minimum_clamps(raw, expected):
    assert env_int(K, 900, env={K: raw}, minimum=60, clamp=True) == expected


@pytest.mark.parametrize("raw,clamp,expected", [("100", False, 5), ("100", True, 10),
                                                ("10", False, 10), ("10", True, 10)])
def test_env_int_maximum(raw, clamp, expected):
    assert env_int(K, 5, env={K: raw}, maximum=10, clamp=clamp) == expected


def test_env_int_none_default_and_range():
    assert env_int(K, None, env={}) is None
    assert env_int(K, None, env={K: "0"}, minimum=1, maximum=65535) is None
    assert env_int(K, None, env={K: "443"}, minimum=1, maximum=65535) == 443


def test_env_int_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv(K, "42")
    assert env_int(K, 1) == 42
    monkeypatch.delenv(K)
    assert env_int(K, 1) == 1


def test_env_int_non_string_mapping_value():
    assert env_int(K, 1, env={K: 12}) == 12  # type: ignore[dict-item]


# ── 2. caller matrix: behaviour preserved per migrated caller ────────────


def _set(monkeypatch, name, raw):
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)


@pytest.mark.parametrize("raw", PROBES)
def test_memory_bounds_positive(monkeypatch, raw):
    from claude_mcp_servers.rl_client import memory_bounds as mb

    _set(monkeypatch, mb.MONITOR_MAX_CONCURRENT_ENV, raw)
    assert mb.monitor_max_concurrent() == _expected_positive(raw, mb.DEFAULT_MONITOR_MAX_CONCURRENT)


@pytest.mark.parametrize("raw", PROBES)
def test_telemetry_writer_payload_cap_positive(monkeypatch, raw):
    from claude_mcp_servers.rl_client import telemetry_writer as tw

    _set(monkeypatch, tw._HUB_PAYLOAD_MAX_BYTES_ENV, raw)
    assert tw._resolve_hub_payload_max_bytes() == _expected_positive(
        raw, tw._HUB_PAYLOAD_MAX_BYTES_DEFAULT)


@pytest.mark.parametrize("raw", PROBES)
def test_rl_retention_any_int(monkeypatch, raw):
    """0 / negative are meaningful here ("bound disabled"), so they pass through."""
    from claude_mcp_servers.rl_client import rl_retention as rr

    _set(monkeypatch, "RL_EVENTS_RETENTION_MAX_ROWS", raw)
    expected = _expected_any(raw, 0)
    plan = rr.compute_retention_plan(now_ms=10**13)
    assert plan.max_rows == (expected if expected > 0 else None)


@pytest.mark.parametrize("raw", PROBES)
def test_drain_gate_any_int(monkeypatch, raw):
    import rl_drain_citations as drain

    _set(monkeypatch, "RL_MIN_ANSWER_TOKENS_FOR_CITATION", raw)
    assert drain._gate_tokens() == _expected_any(raw, 25000)
    _set(monkeypatch, "RL_SOFT_CITATION_MIN_TOKENS", raw)
    assert drain._soft_label_floor_tokens() == _expected_any(raw, 200)
    _set(monkeypatch, "RL_TERMINAL_CITATION_MIN_TOKENS", raw)
    assert drain._terminal_floor_tokens() == _expected_any(raw, 2000)


@pytest.mark.parametrize("raw", PROBES)
def test_code_recover_budget_clamps_negative_to_zero(monkeypatch, raw):
    from claude_mcp_servers.weaviate_mcp import rl_enrichment as rle

    _set(monkeypatch, rle._RL_CODE_RECOVER_MAX_ENV, raw)
    max_nodes, _deadline = rle._rl_code_recover_budget()
    assert max_nodes == max(0, _expected_any(raw, rle._RL_CODE_RECOVER_MAX_DEFAULT))


@pytest.mark.parametrize("raw", PROBES)
def test_compose_timeout_clamps_to_60(raw):
    from vco_lib.install_services_up import compose_timeout_s

    env = {} if raw is None else {"VCT_INSTALL_DOCKER_TIMEOUT": raw}
    try:
        expected = max(60, int((raw or "").strip()))
    except ValueError:
        expected = 900
    assert compose_timeout_s(env) == expected


@pytest.mark.parametrize("raw", PROBES)
def test_gateway_port_range(raw):
    from vco_lib import vscode_settings as vs

    env = {} if raw is None else {vs.PORT_ENV: raw}
    try:
        port = int((raw or "").strip())
        expected = port if 0 < port < 65536 else None
    except ValueError:
        expected = None
    assert vs._read_port_env(env) == expected


@pytest.mark.parametrize("raw", PROBES)
def test_router_cg_cap_positive(monkeypatch, raw):
    import hook_context_router as router

    _set(monkeypatch, "VCO_CG_INJECT_CAP", raw)
    assert router._cg_cap() == _expected_positive(raw, router.CG_SESSION_CAP_DEFAULT)


@pytest.mark.parametrize("raw", PROBES)
def test_model_router_settings_positive(monkeypatch, raw):
    from claude_mcp_servers.model_router import config as mrc

    _set(monkeypatch, mrc.CONTEXT_GUARD_MARGIN_ENV, raw)
    assert mrc.context_guard_margin() == _expected_positive(raw, mrc.CONTEXT_GUARD_MARGIN_DEFAULT)


@pytest.mark.parametrize("raw", PROBES)
def test_wait_guard_repeat_max_positive(tmp_path, raw):
    """``VCO_WAIT_GUARD_REPEAT_MAX`` (read from the hook's env MAPPING): the
    check denies on the max_n-th repeat; a bad value keeps the default."""
    from vco_lib import wait_guard as wg

    env = {} if raw is None else {"VCO_WAIT_GUARD_REPEAT_MAX": raw}
    max_n = _expected_positive(raw, wg.DEFAULT_REPEAT_MAX)
    if max_n > 50:  # "65535"/"65536": accepted as given, far past the loop below
        assert max_n != wg.DEFAULT_REPEAT_MAX
        return
    payload = {"session_id": "s-w6fin", "tool_name": "Bash"}
    denied_at = None
    for i in range(1, max_n + 2):
        if wg._repeat_check(payload, "k", env, tmp_path, 1000.0 + i, "Bash") is not None:
            denied_at = i
            break
    assert denied_at == max_n


# ── 3. parity: stdlib-only documented copies ──────────────────────────────


@pytest.fixture()
def summary_backends(monkeypatch, tmp_path):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KG_SUMMARY_OLLAMA_MODEL", "parity-probe")  # skip the model resolver
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("summary_backends")


@pytest.fixture()
def gen_code_summary(summary_backends, monkeypatch, tmp_path):
    monkeypatch.setenv("KG_PROJECT_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "_w6fin_gen_code_summary", SCRIPTS / "generate-code-summary.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("raw", PROBES)
def test_summary_backends_int_env_matches_env_int(summary_backends, monkeypatch, raw):
    _set(monkeypatch, K, raw)
    assert summary_backends._int_env(K, 3) == env_int(K, 3, minimum=1)


@pytest.mark.parametrize("raw", PROBES)
def test_generate_code_summary_copies_match_env_int(gen_code_summary, raw):
    gen = gen_code_summary
    name = gen._ENV_BATCH_SIZE
    env = {} if raw is None else {name: raw}
    assert gen.resolve_batch_size(env) == env_int(name, gen.DEFAULT_BATCH_SIZE, env=env, minimum=1)
    name = gen._ENV_MAX_PER_RUN
    env = {} if raw is None else {name: raw}
    assert gen.resolve_max_per_run(None, env) == env_int(name, gen.DEFAULT_MAX_PER_RUN, env=env)


def test_copies_name_the_one_home():
    """Each documented copy says which function it must match."""
    for rel in ("templates/scripts/summary_backends.py", "templates/scripts/generate-code-summary.py"):
        assert "must match ``vco_lib/env_values.py::env_int" in (REPO_ROOT / rel).read_text()


# ── env-keys ratchet still sees reads made through env_int ───────────────


def test_env_keys_scanner_sees_env_int_reads():
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    scanner = importlib.import_module("test_v0292_env_keys_documented")
    reads = scanner.collect_env_key_reads()
    # Literal-key call sites (router, wait guard, installer, gateway config)
    # and a same-file-constant call site (memory_bounds).
    for key in ("VCO_CG_INJECT_CAP", "VCO_WAIT_GUARD_REPEAT_MAX", "VCT_INSTALL_DOCKER_TIMEOUT",
                "VCT_MODEL_GATEWAY_KEY_TTL", "VCO_RL_MONITOR_MAX_CONCURRENT"):
        assert key in reads, key
