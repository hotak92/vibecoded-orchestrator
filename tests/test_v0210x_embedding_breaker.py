# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the P299-B4 secondary-slot circuit breaker in EmbeddingService.

The breaker SKIPS an optional secondary slot once it is known to be down (a
missing ``snowflake-arctic-embed2`` used to be CALLED and logged on every embed
— hours of identical 404s, and a WAN round trip per embed for the OpenAI legs),
with a half-open probe so a recovered slot is picked back up without a restart.
Correctness is unchanged: a skipped slot is omitted from the returned dict
exactly as a FAILED one is, and the ACTIVE slot's error path is untouched.

All HTTP is mocked at the adapter level, so nothing reaches Ollama / CodeEmbed /
OpenAI. ``WEAVIATE_URL`` is pinned by ``tests/conftest.py`` to an unroutable
sentinel. The ``DUAL_EMBEDDING_*`` gates are pinned EXPLICITLY in every test —
this machine's shell sets them ambiently.

Coverage:
  (a) 3 consecutive secondary failures → ONE trip line naming the remedy; below
      the threshold the per-call warning is kept; then calls go quiet.
  (b) an OPEN breaker SKIPS the backend call (counted on a fake backend).
  (c) a half-open probe fires after N skipped calls.
  (d) the time leg of the half-open trigger fires too.
  (e) a successful probe CLOSES the breaker.
  (f) a success after 2 failures (below threshold) resets the counter.
  (g) the ACTIVE slot is exempt — its per-call errors are unchanged.
  (h) breaker bookkeeping can never raise into the caller.
  (i) the code-side (codesage) secondary leg is wired too — remedy included.
  (j) the consolidated line re-reports with a GROWING count (nit 4).
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.embedding_providers.codeembed import CodeEmbedAdapter  # noqa: E402
from vco_lib.embedding_providers.ollama import OllamaAdapter  # noqa: E402
from vco_lib.embedding_providers.openai import OpenAIAdapter  # noqa: E402
import vco_lib.embedding_service as es  # noqa: E402
from vco_lib.embedding_service import (  # noqa: E402
    ARCTIC_SECONDARY_MODEL,
    DEFAULT_TEXT_MODEL,
    EmbeddingService,
    SECONDARY_BREAKER_HALF_OPEN_SECS,
    SECONDARY_BREAKER_HALF_OPEN_SKIPS,
    SECONDARY_BREAKER_THRESHOLD,
)

_LOGGER_NAME = "vco_lib.embedding_service"
_QWEN3 = DEFAULT_TEXT_MODEL  # "qwen3-embedding:0.6b"


def _build_service(*, text_model, code_model, ollama_embed, code_embed):
    """An EmbeddingService with fully mocked adapters (no network)."""
    ollama = MagicMock(spec=OllamaAdapter)
    ollama.is_reachable.return_value = True
    ollama.embed.side_effect = ollama_embed

    codee = MagicMock(spec=CodeEmbedAdapter)
    codee.is_reachable.return_value = True
    codee.embed.side_effect = code_embed

    oa = MagicMock(spec=OpenAIAdapter)
    oa.is_reachable.return_value = False

    return EmbeddingService(
        project_root=None,
        ollama_url="http://localhost:11435",
        code_embed_url="http://localhost:11440",
        text_model_id=text_model,
        code_model_id=code_model,
        openai_api_key="",
        ollama_adapter=ollama,
        code_adapter=codee,
        openai_adapter=oa,
    )


def _pin_dual(monkeypatch):
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "true")
    # Keep the arctic secondary OFF unless a test wants it: the ambient shell
    # sets it true, and an inherited value would add an unexpected secondary.
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "false")


def _records(caplog):
    return [r for r in caplog.records if r.name == _LOGGER_NAME]


def _messages(caplog):
    return [r.getMessage() for r in _records(caplog)]


def _qwen3_secondary_service(monkeypatch, calls, *, fail_state, code_embed=None):
    """Active = arctic, so the qwen3 SECONDARY is the one that (mis)behaves.

    ``calls`` records every ``(model)`` handed to the Ollama backend;
    ``fail_state`` is a mutable dict ``{"fail": bool}`` controlling whether the
    qwen3 embed raises.
    """

    def _embed(model, text, num_ctx=None):
        calls.append(model)
        if "qwen3" in model:
            if fail_state["fail"]:
                raise RuntimeError("404 model 'qwen3-embedding:0.6b' not found")
            return [0.1] * 4
        return [0.1] * 4

    return _build_service(
        text_model=ARCTIC_SECONDARY_MODEL,
        code_model="codesage/codesage-large-v2",
        ollama_embed=_embed,
        code_embed=code_embed or (lambda text, is_query=False, task=None: [0.0]),
    )


def _qwen3_calls(calls):
    return [m for m in calls if "qwen3" in m]


def test_trip_logs_one_line_with_remedy_then_goes_quiet(monkeypatch, caplog):
    """(a) 3 failures trip the breaker: 2 per-call warnings + ONE consolidated
    line naming the remedy; further calls add no lines."""
    _pin_dual(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    calls: list[str] = []
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state={"fail": True})

    for _ in range(SECONDARY_BREAKER_THRESHOLD):
        svc.embed_text_all_configured("x" * 20)

    msgs = _messages(caplog)
    trips = [m for m in msgs if "suppressed after" in m]
    assert len(trips) == 1, msgs
    assert trips[0].startswith("secondary slot qwen3_embed failing: suppressed")
    assert "(count=3)" in trips[0]
    # SF-3: the consolidated line names a concrete remedy.
    assert "ollama pull qwen3-embedding:0.6b" in trips[0]

    per_call = [m for m in msgs if m.startswith("qwen3 fallback embedding failed")]
    assert len(per_call) == SECONDARY_BREAKER_THRESHOLD - 1, msgs

    # Further calls are SKIPPED and add no log lines (well under REPORT_EVERY).
    before = len(_records(caplog))
    for _ in range(20):
        svc.embed_text_all_configured("x" * 20)
    assert len(_records(caplog)) == before


def test_open_breaker_skips_the_backend_call(monkeypatch):
    """(b) the whole point: once OPEN, the failing backend is NOT called."""
    _pin_dual(monkeypatch)
    calls: list[str] = []
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state={"fail": True})

    for _ in range(SECONDARY_BREAKER_THRESHOLD):
        svc.embed_text_all_configured("x" * 20)
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD

    # 20 more calls, well below the half-open threshold → skipped, no backend.
    for _ in range(20):
        svc.embed_text_all_configured("x" * 20)
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD, (
        "the open breaker must not call the failing backend"
    )


def test_half_open_probe_after_n_skipped_calls(monkeypatch):
    """(c) after HALF_OPEN_SKIPS skipped calls, ONE probe is let through."""
    _pin_dual(monkeypatch)
    calls: list[str] = []
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state={"fail": True})

    for _ in range(SECONDARY_BREAKER_THRESHOLD):
        svc.embed_text_all_configured("x")
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD

    # The first HALF_OPEN_SKIPS-1 post-trip calls are skips (no backend).
    for _ in range(SECONDARY_BREAKER_HALF_OPEN_SKIPS - 1):
        svc.embed_text_all_configured("x")
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD

    # The next call is the probe → the backend is called again.
    svc.embed_text_all_configured("x")
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD + 1


def test_half_open_probe_via_time_leg(monkeypatch):
    """(d) the time leg of the trigger fires without waiting 60 s."""
    _pin_dual(monkeypatch)
    calls: list[str] = []
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state={"fail": True})

    for _ in range(SECONDARY_BREAKER_THRESHOLD):
        svc.embed_text_all_configured("x")
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD

    # Age the open clock past the cooldown; the next call must probe.
    svc._secondary_breakers["qwen3_embed"].opened_at -= (
        SECONDARY_BREAKER_HALF_OPEN_SECS + 1.0
    )
    svc.embed_text_all_configured("x")
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD + 1


def test_successful_probe_closes_the_breaker(monkeypatch):
    """(e) a successful half-open probe closes the breaker for good."""
    _pin_dual(monkeypatch)
    calls: list[str] = []
    fail = {"fail": True}
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state=fail)

    for _ in range(SECONDARY_BREAKER_THRESHOLD):
        svc.embed_text_all_configured("x")
    assert "qwen3_embed" in svc._secondary_breakers

    # The model comes back; push to the probe, which now succeeds → CLOSED.
    fail["fail"] = False
    for _ in range(SECONDARY_BREAKER_HALF_OPEN_SKIPS):
        svc.embed_text_all_configured("x")
    assert "qwen3_embed" not in svc._secondary_breakers, (
        "a successful probe must drop the breaker state (CLOSED)"
    )

    # A normal call now attempts the backend again (no skip).
    before = len(_qwen3_calls(calls))
    svc.embed_text_all_configured("x")
    assert len(_qwen3_calls(calls)) == before + 1


def test_success_after_two_failures_resets_the_counter(monkeypatch, caplog):
    """(f) below the threshold, a success resets the counter (no trip)."""
    _pin_dual(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    fail = {"fail": True}
    calls: list[str] = []
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state=fail)

    svc.embed_text_all_configured("x")   # fail 1
    svc.embed_text_all_configured("x")   # fail 2
    fail["fail"] = False
    svc.embed_text_all_configured("x")   # SUCCESS → reset
    fail["fail"] = True
    svc.embed_text_all_configured("x")   # fail 1
    svc.embed_text_all_configured("x")   # fail 2 — still below

    assert not any("suppressed after" in m for m in _messages(caplog))
    # The success reset the count: the 2 later failures read 2, not 4.
    assert svc._secondary_breakers["qwen3_embed"].failures == 2
    assert not svc._secondary_breakers["qwen3_embed"].open


def test_active_slot_is_exempt_from_the_breaker(monkeypatch, caplog):
    """(g) the ACTIVE slot's per-call error path is unchanged — no breaker."""
    _pin_dual(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    calls: list[str] = []

    def _always_fail(model, text, num_ctx=None):
        calls.append(model)
        raise RuntimeError("backend down")

    # Active = qwen3, so there is NO secondary fan-out at all here.
    svc = _build_service(
        text_model=_QWEN3,
        code_model="codesage/codesage-large-v2",
        ollama_embed=_always_fail,
        code_embed=lambda text, is_query=False, task=None: [0.0],
    )

    for _ in range(5):
        svc.embed_text_all_configured("x")

    msgs = _messages(caplog)
    active = [m for m in msgs if m.startswith("Active text backend failed")]
    assert len(active) == 5, msgs
    assert not any("suppressed after" in m for m in msgs), (
        "the active slot must never be routed through the breaker"
    )
    # It is still CALLED every time (nothing gated the active path).
    assert len(calls) == 5


def test_breaker_bookkeeping_never_raises_into_the_caller(monkeypatch):
    """(h) any error inside the breaker state is swallowed — the embed call
    still returns normally (correctness unchanged)."""
    _pin_dual(monkeypatch)
    calls: list[str] = []
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state={"fail": True})

    class _Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("state container boom")

        def __setitem__(self, *a, **k):
            raise RuntimeError("state container boom")

    svc._secondary_breakers = _Boom()

    result = svc.embed_text_all_configured("x" * 20)
    assert "arctic2_embed" in result          # the active slot still works
    assert "qwen3_embed" not in result        # the secondary still failed


def test_code_secondary_leg_uses_the_breaker_too(monkeypatch, caplog):
    """(i) the code-side (codesage) secondary leg is wired to the breaker."""
    _pin_dual(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    code_calls: list[str] = []

    def _code_fail(text, is_query=False, task=None):
        code_calls.append(text)
        raise RuntimeError("CodeEmbed service exploded")

    # Active code slot = qwen3_embed (CPU fallback) → codesage is a secondary.
    svc = _build_service(
        text_model=_QWEN3,
        code_model=_QWEN3,
        ollama_embed=lambda model, text, num_ctx=None: [0.1] * 4,
        code_embed=_code_fail,
    )

    for _ in range(SECONDARY_BREAKER_THRESHOLD):
        svc.embed_code_all_configured("def f(): pass")

    trips = [m for m in _messages(caplog) if "suppressed after" in m]
    assert len(trips) == 1, _messages(caplog)
    assert "codesage_embed" in trips[0]
    assert "CODE_EMBED_SERVICE_URL" in trips[0]  # SF-3 remedy

    # The open breaker skips the codesage backend (call count frozen).
    before = len(code_calls)
    for _ in range(20):
        svc.embed_code_all_configured("def f(): pass")
    assert len(code_calls) == before


def test_report_repeats_with_a_growing_count(monkeypatch, caplog):
    """(j) nit 4: the suppressed count keeps growing, re-reported periodically,
    so a reader can tell 4 suppressed calls from 40 000."""
    _pin_dual(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    # Smaller cadence so the test does not need 500 calls; the LOGIC is the same.
    monkeypatch.setattr(es, "SECONDARY_BREAKER_REPORT_EVERY", 20)
    calls: list[str] = []
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state={"fail": True})

    for _ in range(SECONDARY_BREAKER_THRESHOLD):
        svc.embed_text_all_configured("x")
    for _ in range(30):                       # skips → periodic re-reports
        svc.embed_text_all_configured("x")

    trips = [m for m in _messages(caplog) if "suppressed after" in m]
    assert len(trips) >= 2, trips
    counts = []
    for m in trips:
        match = re.search(r"count=(\d+)", m)
        assert match is not None
        counts.append(int(match.group(1)))
    assert counts == sorted(counts) and counts[-1] > counts[0], counts


class _LostProbe(BaseException):
    """A BaseException (the ``asyncio.CancelledError`` shape) that escapes a
    call-site's ``except Exception`` arm, stranding the probe."""


def _call_swallowing_base(svc):
    try:
        svc.embed_text_all_configured("x")
    except BaseException:  # noqa: BLE001 — the probe was cancelled mid-flight
        pass


def test_a_lost_probe_does_not_skip_the_slot_for_the_process_life(monkeypatch):
    """SF-N1: a probe lost to a BaseException must self-heal after the
    cool-down, never skip the slot for the process life."""
    _pin_dual(monkeypatch)
    calls: list[str] = []
    state = {"baseexc": False}

    def _embed(model, text, num_ctx=None):
        calls.append(model)
        if "qwen3" in model:
            if state["baseexc"]:
                raise _LostProbe("probe cancelled mid-flight")
            raise RuntimeError("404 model 'qwen3-embedding:0.6b' not found")
        return [0.1] * 4

    svc = _build_service(
        text_model=ARCTIC_SECONDARY_MODEL,
        code_model="codesage/codesage-large-v2",
        ollama_embed=_embed,
        code_embed=lambda text, is_query=False, task=None: [0.0],
    )

    # Trip the breaker with ordinary Exception failures.
    for _ in range(SECONDARY_BREAKER_THRESHOLD):
        svc.embed_text_all_configured("x")
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD

    # Grant a probe (age the clock) and let it die to a BaseException — the
    # call-site's ``except Exception`` does NOT catch it, so neither note runs.
    svc._secondary_breakers["qwen3_embed"].opened_at -= (
        SECONDARY_BREAKER_HALF_OPEN_SECS + 1.0
    )
    state["baseexc"] = True
    _call_swallowing_base(svc)
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD + 1  # probe ran
    assert svc._secondary_breakers["qwen3_embed"].probing is True, "probe stranded"

    # The stale probe must be treated as lost → a FRESH probe reaches the
    # backend. (Pre-SF-N1 this call is skipped forever.)
    svc._secondary_breakers["qwen3_embed"].opened_at -= (
        SECONDARY_BREAKER_HALF_OPEN_SECS + 1.0
    )
    _call_swallowing_base(svc)
    assert len(_qwen3_calls(calls)) == SECONDARY_BREAKER_THRESHOLD + 2, (
        "a stranded probe must be cleared and a fresh one granted"
    )


class _SlowProbeState:
    """A duck-typed breaker state whose ``probing`` access sleeps briefly.

    The sleep yields the GIL BETWEEN the ``probing`` read and the write, so an
    UNLOCKED check-then-set is exposed deterministically: several threads read
    the pre-set value before any of them writes it.
    """

    def __init__(self, opened_at: float) -> None:
        self.open = True
        self.skipped = 0
        self.skips_since_probe = 0
        self.opened_at = opened_at
        self._probing = False

    @property
    def probing(self) -> bool:
        time.sleep(0.005)
        return self._probing

    @probing.setter
    def probing(self, value: bool) -> None:
        time.sleep(0.005)
        self._probing = value


def test_concurrent_should_skip_grants_at_most_one_probe(monkeypatch):
    """Nit 2: the check-then-set on ``probing`` is locked — N concurrent
    ``should_skip`` calls grant AT MOST one probe."""
    _pin_dual(monkeypatch)
    calls: list[str] = []
    svc = _qwen3_secondary_service(monkeypatch, calls, fail_state={"fail": True})
    # OPEN, clock aged so the very first entrant sees the trigger satisfied.
    # ``_SlowProbeState`` duck-types the real state (see the class docstring).
    svc._secondary_breakers["qwen3_embed"] = cast(
        Any,
        _SlowProbeState(
            opened_at=time.monotonic() - (SECONDARY_BREAKER_HALF_OPEN_SECS + 1.0)
        ),
    )

    n = 4
    results: list[bool] = []
    results_lock = threading.Lock()
    start = threading.Barrier(n)

    def _worker() -> None:
        start.wait()  # maximise contention: all threads enter together
        granted = svc._secondary_breaker_should_skip("qwen3_embed")
        with results_lock:
            results.append(granted)

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    probes = [r for r in results if r is False]
    assert len(probes) == 1, f"exactly one probe may be granted, got {len(probes)}"


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))