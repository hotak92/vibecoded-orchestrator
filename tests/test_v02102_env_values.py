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


# ══ v0.2.102 W6-R5FIX (review R5-2): the readers the FIN pass left ════════
#
# Each expected value is computed by an ORACLE that restates the private
# reader the call site had before the migration — so these tests pass on the
# pre-migration tree too (the proof that the move changed nothing) and fail
# the moment a migrated call site drifts.


def _old_int_positive_or(raw, default):
    """``int(stripped)``; > 0 else ``default`` (blank / garbage -> default)."""
    try:
        v = int((raw or "").strip())
    except ValueError:
        return default
    return v if v > 0 else default


@pytest.mark.parametrize("raw", PROBES)
def test_gateway_explicit_port(monkeypatch, raw):
    from claude_mcp_servers.model_router import config as mrc

    _set(monkeypatch, "VCT_MODEL_GATEWAY_PORT", raw)
    try:
        v = int((raw or "").strip())
        expected = v if 1 <= v <= 65535 else None
    except ValueError:
        expected = None
    assert mrc.explicit_port() == expected


@pytest.mark.parametrize("raw", PROBES)
def test_prompt_min_chars_positive(monkeypatch, raw):
    from vco_lib import inject_intent as ii

    _set(monkeypatch, ii.PROMPT_MIN_CHARS_ENV, raw)
    assert ii._prompt_min_chars() == _old_int_positive_or(raw, ii.PROMPT_MIN_CHARS)


@pytest.mark.parametrize("raw", PROBES)
def test_kg_overfetch_at_least_one(monkeypatch, raw):
    from claude_mcp_servers.weaviate_mcp import rl_state

    _set(monkeypatch, "KG_OVERFETCH_MULTIPLIER", raw)
    assert rl_state._resolve_overfetch(2) == _old_int_positive_or(raw, 2)


@pytest.mark.parametrize("raw", PROBES)
def test_waiting_parent_pid_positive_or_none(monkeypatch, raw):
    from vco_lib import install_companions as ic

    _set(monkeypatch, ic.ENV_PARENT_WAITS, raw)
    assert ic.waiting_parent_pid() == _old_int_positive_or(raw, None)


@pytest.mark.parametrize("raw", PROBES)
def test_enrichment_grpc_port_any_int(monkeypatch, raw):
    from vco_lib import embedding_enrichment as ee

    _set(monkeypatch, "GRPC_PORT", raw)
    assert ee._grpc_port() == _expected_any(raw, 50052)


@pytest.mark.parametrize("raw", PROBES)
def test_edge_timeout_nonpositive_means_none(monkeypatch, raw):
    from vco_lib import schema_migration_runner as smr

    _set(monkeypatch, "VCT_EDGE_TIMEOUT_SECS", raw)
    try:
        v = int((raw or "").strip())
        expected = None if v <= 0 else v
    except ValueError:
        expected = 3600
    assert smr._resolve_edge_timeout() == expected


@pytest.mark.parametrize("raw", PROBES)
def test_generate_num_ctx_says_a_bad_override(monkeypatch, capsys, raw):
    from vco_lib import ollama_generate as og

    _set(monkeypatch, og.GENERATE_NUM_CTX_ENV, raw)
    monkeypatch.setattr(og, "_warned", set())
    got = og.generate_num_ctx()
    assert got == _old_int_positive_or(raw, og.GENERATE_NUM_CTX_DEFAULT)
    err = capsys.readouterr().err
    stripped = (raw or "").strip()
    bad = bool(stripped) and _old_int_positive_or(raw, None) is None
    assert ("ignoring" in err) == bad, (raw, err)


# ── env_float: the helper's contract ──────────────────────────────────────

from vco_lib.env_values import env_float  # noqa: E402
import math  # noqa: E402

KF = "VCO_W6R5_PROBE_FLOAT"
FLOAT_PROBES = [None, "", "   ", "abc", "1,5", "1.5", " 2.5 ", "-3", "-0.5", "0", "0.0",
                "7", "1e3", "1_0", "nan", "inf", "-inf"]


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b and type(a) is type(b)


def _parse_or(raw, default):
    """The shared parse every replaced reader did: unset / blank / garbage
    -> ``default``, else ``float(stripped)``."""
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_env_float_unbounded(raw):
    env = {} if raw is None else {KF: raw}
    assert _same(env_float(KF, 4.5, env=env), _parse_or(raw, 4.5))


@pytest.mark.parametrize("raw,kw,expected", [
    ("-1", {"minimum": 0}, 9.0), ("0", {"minimum": 0}, 0.0), ("-1", {"minimum": 0, "clamp": True}, 0),
    ("0", {"above": 0}, 9.0), ("0.01", {"above": 0}, 0.01), ("nan", {"above": 0}, 9.0),
    ("5", {"maximum": 2}, 9.0), ("5", {"maximum": 2, "clamp": True}, 2),
    ("-5", {"above": 0, "minimum": -9, "clamp": True}, 9.0),
])
def test_env_float_bounds(raw, kw, expected):
    assert env_float(KF, 9.0, env={KF: raw}, **kw) == expected


def test_env_float_nan_passes_plain_bounds():
    """The plain ``<`` / ``>`` the replaced readers used: nan is inside
    ``minimum``/``maximum`` (callers that must reject it keep their own
    ``>=`` or ``max()``), and outside ``above``."""
    assert math.isnan(env_float(KF, 1.0, env={KF: "nan"}, minimum=0, maximum=1))
    assert env_float(KF, 1.0, env={KF: "nan"}, above=0) == 1.0


def test_env_float_none_default_and_os_environ(monkeypatch):
    monkeypatch.delenv(KF, raising=False)
    assert env_float(KF, None) is None
    monkeypatch.setenv(KF, " 2.5 ")
    assert env_float(KF, None) == 2.5


# ── env_float: caller matrix (oracle = the pre-migration reader) ──────────


def _old_positive(raw, default):
    v = _parse_or(raw, None)
    return v if v is not None and v > 0 else default


def _old_lt0_default(raw, default):
    v = _parse_or(raw, None)
    if v is None:
        return default
    return default if v < 0 else v


def _old_ge0_or(raw, default):
    v = _parse_or(raw, None)
    return v if v is not None and v >= 0 else default


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_retry_after_tolerances(monkeypatch, raw):
    from vco_lib import retry_after as ra

    for env_name in (ra.EMBED_503_TOLERANCE_ENV, ra.EMBED_503_INTERACTIVE_TOLERANCE_ENV,
                     ra.EMBED_INTERACTIVE_REFUSED_TOLERANCE_ENV, ra.EMBED_503_RETRY_DELAY_ENV):
        monkeypatch.delenv(env_name, raising=False)
    _set(monkeypatch, ra.EMBED_503_TOLERANCE_ENV, raw)
    assert _same(ra.resolve_503_tolerance(), _old_lt0_default(raw, ra.DEFAULT_503_TOLERANCE_SECS))
    monkeypatch.delenv(ra.EMBED_503_TOLERANCE_ENV, raising=False)
    batch = ra.DEFAULT_503_TOLERANCE_SECS
    _set(monkeypatch, ra.EMBED_503_INTERACTIVE_TOLERANCE_ENV, raw)
    assert _same(ra.resolve_503_interactive_tolerance(),
                 min(_old_ge0_or(raw, ra.DEFAULT_503_INTERACTIVE_TOLERANCE_SECS), batch))
    _set(monkeypatch, ra.EMBED_INTERACTIVE_REFUSED_TOLERANCE_ENV, raw)
    assert _same(ra.resolve_refused_tolerance(interactive=True),
                 min(_old_ge0_or(raw, ra.DEFAULT_INTERACTIVE_REFUSED_TOLERANCE_SECS), batch))
    _set(monkeypatch, ra.EMBED_503_RETRY_DELAY_ENV, raw)
    first = _parse_or(raw, None)
    expected = None if first is None else (0.0 if first < 0 else first) / 2.0
    assert _same(ra.resolve_503_delay_scale(), expected)


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_disk_min_free_positive(monkeypatch, raw):
    from vco_lib import doctor

    _set(monkeypatch, doctor.DISK_MIN_FREE_ENV, raw)
    gib = _old_positive(raw, None)
    if gib is None:
        d = doctor.DISK_MIN_FREE_GB_DEFAULT
        expected = (int(d * doctor._GIB), d, False)
    else:
        expected = (int(gib * doctor._GIB), gib, True) if not math.isinf(gib) else None
    if expected is None:  # int(inf) raises before and after alike
        with pytest.raises(OverflowError):
            doctor.disk_min_free_bytes()
        return
    assert doctor.disk_min_free_bytes() == expected


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_embed_request_timeout_says_bad_values(monkeypatch, caplog, raw):
    import logging

    from vco_lib import embedding_service as es

    _set(monkeypatch, es.EMBED_REQUEST_TIMEOUT_ENV, raw)
    with caplog.at_level(logging.WARNING, logger=es.logger.name):
        got = es._resolve_embed_request_timeout()
    v = _parse_or(raw, None)
    stripped = (raw or "").strip()
    if v is None:
        expected, says = es.DEFAULT_EMBED_REQUEST_TIMEOUT_SECS, ("not a number" if stripped else "")
    elif v <= 0:
        expected, says = es.DEFAULT_EMBED_REQUEST_TIMEOUT_SECS, "not positive"
    else:
        expected, says = v, ""
    assert _same(float(got), float(expected))
    text = caplog.text
    assert (says in text) if says else ("not a number" not in text and "not positive" not in text)


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_rl_plain_float_ttls(monkeypatch, raw):
    """The ``float(env or DEFAULT)`` family: any float, blank/garbage ->
    default (deferred emit, identity cache, recovery deadline, drain age)."""
    from claude_mcp_servers.rl_client import deferred_emit as de
    from claude_mcp_servers.weaviate_mcp import rl_enrichment as rle
    import rl_drain_citations as drain

    _set(monkeypatch, de.ORPHAN_TTL_ENV, raw)
    assert _same(de._orphan_ttl_s(), _parse_or(raw, de._ORPHAN_TTL_DEFAULT_S))
    _set(monkeypatch, rle.IDENTITY_CACHE_TTL_ENV, raw)
    assert _same(rle._identity_cache_ttl_s(), _parse_or(raw, rle._IDENTITY_CACHE_TTL_DEFAULT_S))
    _set(monkeypatch, rle._RL_CODE_RECOVER_DEADLINE_ENV, raw)
    assert _same(rle._rl_code_recover_budget()[1],
                 _parse_or(raw, rle._RL_CODE_RECOVER_DEADLINE_DEFAULT_S))
    _set(monkeypatch, "RL_TERMINAL_CITATION_AGE_SECONDS", raw)
    assert _same(drain._terminal_age_seconds(), _parse_or(raw, 1800.0))


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_weaviate_ready_deadline(monkeypatch, raw):
    import install

    _set(monkeypatch, "WEAVIATE_READY_TIMEOUT", raw)
    assert _same(install._weaviate_ready_deadline_seconds(),
                 _parse_or(raw, float(install.WEAVIATE_READY_TIMEOUT)))


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_gateway_fallback_retry_after_positive(monkeypatch, raw):
    from claude_mcp_servers.model_router import server as mrs

    _set(monkeypatch, mrs.FALLBACK_RETRY_AFTER_MAX_ENV, raw)
    assert _same(mrs.fallback_retry_after_max_s(),
                 _old_positive(raw, mrs.FALLBACK_RETRY_AFTER_MAX_DEFAULT_S))


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_tier_safe_float_says_garbage(monkeypatch, caplog, raw):
    import logging

    from claude_mcp_servers.weaviate_mcp import kg_query_core as kqc

    _set(monkeypatch, KF, raw)
    with caplog.at_level(logging.WARNING, logger=kqc.logger.name):
        got = kqc._safe_float(KF, "0.42")
    assert _same(got, _parse_or(raw, 0.42))
    garbage = bool((raw or "").strip()) and _parse_or(raw, None) is None
    assert ("not a valid float" in caplog.text) == garbage


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_code_ranking_env_override(raw):
    from claude_mcp_servers.weaviate_mcp import code_ranking as cr

    env = {} if raw is None else {KF: raw}
    assert _same(cr._coerce_env_float(env, KF), _parse_or(raw, None))
    assert cr._coerce_env_float(None, KF) is None
    assert cr._coerce_env_float({KF: 0.25}, KF) == 0.25  # a fixture mapping holding a float


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_rl_retention_min_interval_any_float(monkeypatch, raw):
    from claude_mcp_servers.rl_client import rl_retention as rr

    seen: list = []
    monkeypatch.setattr(rr, "_throttle_ready", lambda interval, now: seen.append(interval) or False)
    monkeypatch.setattr(rr, "retention_disabled", lambda: False)
    _set(monkeypatch, rr._ENV_MIN_INTERVAL_S, raw)
    out = rr.maybe_run_retention(now_ms=10**13, prune_fn=lambda **k: 0)
    assert out["skipped"] == "throttled" and len(seen) == 1
    assert _same(seen[0], _parse_or(raw, rr._DEFAULT_MIN_INTERVAL_S))


# ── env_float / env_int: documented copies (no vco_lib where they run) ────


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_summary_backends_float_env_matches(summary_backends, monkeypatch, raw):
    _set(monkeypatch, KF, raw)
    v = env_float(KF, None)
    expected = v if v is not None and v >= 0 else 3.0
    assert _same(summary_backends._float_env(KF, 3.0), expected)


@pytest.fixture()
def code_embed_server():
    import claude_mcp_servers.code_embedding_service.server as ces

    return ces


@pytest.mark.parametrize("raw", FLOAT_PROBES)
@pytest.mark.parametrize("kw", [{}, {"minimum": 0}, {"minimum": 0, "clamp": True}])
def test_code_embed_env_float_copy_matches(code_embed_server, monkeypatch, raw, kw):
    _set(monkeypatch, KF, raw)
    assert _same(code_embed_server._env_float(KF, 30.0, **kw), env_float(KF, 30.0, **kw))


@pytest.mark.parametrize("raw", PROBES)
@pytest.mark.parametrize("kw", [{}, {"minimum": 0}, {"minimum": 0, "clamp": True}])
def test_code_embed_env_int_copy_matches(code_embed_server, monkeypatch, raw, kw):
    _set(monkeypatch, K, raw)
    assert code_embed_server._env_int(K, 12, **kw) == env_int(K, 12, **kw)


@pytest.mark.parametrize("raw", FLOAT_PROBES)
def test_code_embed_resolvers_keep_their_rules(code_embed_server, monkeypatch, raw):
    ces = code_embed_server
    _set(monkeypatch, "CODE_EMBED_IDLE_UNLOAD_SECS", raw)
    assert _same(ces._resolve_idle_unload_secs(), _old_lt0_default(raw, 300.0))
    _set(monkeypatch, "CODE_EMBED_QUEUE_WAIT_SECS", raw)
    assert _same(ces._resolve_queue_wait_secs(), _old_lt0_default(raw, 600.0))
    _set(monkeypatch, "CODE_EMBED_LOAD_OOM_BACKOFF_SECS", raw)
    assert _same(ces._resolve_load_oom_backoff_secs(), max(0.0, _parse_or(raw, 30.0)))
    _set(monkeypatch, "CODE_EMBED_UNLOAD_FREED_MIN_MB", raw)
    assert _same(ces._resolve_unload_freed_min_mb(), max(0.0, _parse_or(raw, 64.0)))


@pytest.mark.parametrize("raw", PROBES)
def test_code_embed_oom_retries_clamps(code_embed_server, monkeypatch, raw):
    _set(monkeypatch, "CODE_EMBED_LOAD_OOM_RETRIES", raw)
    assert code_embed_server._resolve_load_oom_retries() == max(0, _expected_any(raw, 12))


def test_float_copies_name_the_one_home():
    assert "must match ``v = vco_lib/env_values.py::env_float" in (
        REPO_ROOT / "templates/scripts/summary_backends.py").read_text()
    src = (REPO_ROOT / "claude_mcp_servers/code_embedding_service/server.py").read_text()
    assert "must match ``vco_lib/env_values.py::env_float``" in src
    assert "must match ``vco_lib/env_values.py::env_int``" in src


def test_env_keys_scanner_sees_env_float_reads():
    """Keys read only through env_float (literal and same-file constant)."""
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    scanner = importlib.import_module("test_v0292_env_keys_documented")
    reads = scanner.collect_env_key_reads()
    for key in ("VCO_INJECT_LEG_TIMEOUT_S", "VCO_INJECT_BUDGET_S", "VCO_RL_EMIT_ORPHAN_TTL_S",
                "VCO_RL_IDENTITY_CACHE_TTL_S", "VCO_RESYNC_DRIVER_WAIT_S"):
        assert key in reads, key
