# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-Q — summary-backend circuit breaker + non-answer rejection.

Two field defects, both live at the start of this cycle:

1. **No usage-limit circuit breaker.** ``cli_available()`` cached only the
   INITIAL smoke test, so an account that hit its cap mid-backfill kept
   spawning a real ``claude -p`` per remaining node (426 headless sessions
   observed in one run) instead of falling through to Ollama. These tests
   pin the classification policy (a 429 and a 529 are NOT the same
   condition), the demotion, the cooldown, the cross-process latch, and
   the expiry — a permanent latch on a transient 529 would be its own bug.

2. **43% of cached summaries were model NON-ANSWERS**, frozen in place by
   the content-hash gate. These tests pin both halves of the fix: reject at
   write time, and treat an already-poisoned row as unsatisfied so it
   regenerates without the user guessing which rows are bad. The hash gate
   itself must still skip healthy unchanged nodes — that is what makes sync
   ~1.1 s/node instead of ~34 s/node.

Plus the root cause of (2): the prompt used to be an ARGV element, which on
Windows is re-parsed by ``cmd.exe`` (npm ships ``claude`` as a ``.cmd``
shim) and truncated at the first newline — leaving the model with the
system prompt alone, which is why it answered "Ready. What do you need
summarized?". The prompt now goes over stdin.

All synthetic: no LLM call, no Weaviate, no network, no real user state.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "templates" / "scripts"
_SB_PATH = _SCRIPTS / "summary_backends.py"
_KG_PATH = _SCRIPTS / "generate-kg-summary.py"
_CODE_PATH = _SCRIPTS / "generate-code-summary.py"

_SUMMARY_ENV = (
    "KG_SUMMARY_BACKEND", "CODE_SUMMARY_BACKEND", "KG_SUMMARY_TIMEOUT",
    "VCO_SUMMARY_BREAKER", "VCO_SUMMARY_BREAKER_COOLDOWN",
    "VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN",
    "VCO_SUMMARY_BREAKER_CAPACITY_STRIKES",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
)


def _load(name: str, path: Path) -> types.ModuleType:
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated VCT state dir — the breaker latch must never touch ~/.vct."""
    target = tmp_path / "vct-state"
    target.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(target))
    for key in _SUMMARY_ENV:
        monkeypatch.delenv(key, raising=False)
    return target


@pytest.fixture()
def sb(state_dir: Path):
    """Fresh summary_backends with a captured logger and empty caches."""
    mod = _load("_wpq_sb", _SB_PATH)
    mod.reset_backend_cache()
    mod.reset_breaker(persisted=True)
    lines: list[str] = []
    mod.set_logger(lines.append)
    setattr(mod, "log_lines", lines)  # dynamic test handle
    return mod


def _pin_probes(monkeypatch, mod, *, cli=False, ollama=False,
                openai=False, api=False) -> None:
    monkeypatch.setattr(mod, "cli_available", lambda: cli)
    monkeypatch.setattr(mod, "ollama_available", lambda: ollama)
    monkeypatch.setattr(mod, "openai_available", lambda: openai)
    monkeypatch.setattr(mod, "api_available", lambda: api)


# ══════════════════════════════════════════════════════════════════════
# Failure classification — the four conditions must stay distinct
# ══════════════════════════════════════════════════════════════════════
class TestClassification:
    @pytest.mark.parametrize("status,expected", [
        (429, "rate_limit"),
        (401, "auth"),
        (403, "auth"),
        (529, "capacity"),
        (503, "capacity"),
        (500, "capacity"),
        (418, "other"),
    ])
    def test_status_codes(self, sb, status, expected):
        reason, _ = sb.classify_backend_failure(status=status)
        assert reason == expected

    @pytest.mark.parametrize("text,expected", [
        ("Claude AI usage limit reached", "rate_limit"),
        ("rate_limit_error: too many requests", "rate_limit"),
        ("Your credit balance is too low", "rate_limit"),
        ("Invalid API key - please run /login", "auth"),
        ("authentication_error", "auth"),
        ("Overloaded", "capacity"),
        ("upstream request timed out", "capacity"),
        ("Traceback: KeyError 'content'", "other"),
        ("", "other"),
    ])
    def test_text_signals(self, sb, text, expected):
        reason, _ = sb.classify_backend_failure(text=text)
        assert reason == expected, text

    def test_signal_is_a_keyword_never_raw_text(self, sb):
        """Privacy: only the matched token is recorded, never the failure
        text (which on these paths can quote node content)."""
        secret = "usage limit reached; context was: SECRET-NODE-BODY"
        reason, signal = sb.classify_backend_failure(text=secret)
        assert reason == "rate_limit"
        assert "SECRET-NODE-BODY" not in signal
        sb.trip_backend("cli", reason, signal)
        stored = (sb._breaker_path()).read_text(encoding="utf-8")
        assert "SECRET-NODE-BODY" not in stored


# ══════════════════════════════════════════════════════════════════════
# Breaker policy: what trips, what does not, for how long
# ══════════════════════════════════════════════════════════════════════
class TestBreakerPolicy:
    def test_rate_limit_demotes_on_the_first_occurrence(self, sb):
        assert sb.trip_backend("cli", "rate_limit", "usage limit") is True
        assert sb.breaker_state("cli") is not None

    def test_auth_demotes_on_the_first_occurrence(self, sb):
        assert sb.trip_backend("cli", "auth", "401") is True
        assert sb.breaker_state("cli") is not None

    def test_other_never_demotes(self, sb):
        for _ in range(10):
            assert sb.trip_backend("cli", "other", "") is False
        assert sb.breaker_state("cli") is None

    def test_single_capacity_failure_does_not_demote(self, sb):
        """A permanent (or even a 15-minute) latch on ONE transient 529
        would be its own bug — the endpoint recovers by itself."""
        assert sb.trip_backend("cli", "capacity", "529") is False
        assert sb.breaker_state("cli") is None

    def test_capacity_demotes_after_the_configured_strikes(self, sb):
        assert sb.trip_backend("cli", "capacity", "529") is False
        assert sb.trip_backend("cli", "capacity", "529") is False
        assert sb.trip_backend("cli", "capacity", "529") is True
        record = sb.breaker_state("cli")
        assert record is not None and record["reason"] == "capacity"

    def test_a_success_resets_the_capacity_strike_count(self, sb):
        sb.trip_backend("cli", "capacity", "529")
        sb.trip_backend("cli", "capacity", "529")
        sb.clear_backend_trip("cli")           # a call succeeded
        assert sb.trip_backend("cli", "capacity", "529") is False
        assert sb.breaker_state("cli") is None

    def test_capacity_cooldown_is_shorter_than_the_rate_limit_one(self, sb):
        assert sb.cooldown_for("capacity") < sb.cooldown_for("rate_limit")
        assert sb.cooldown_for("rate_limit") == sb.DEFAULT_BREAKER_COOLDOWN_S
        assert sb.cooldown_for("capacity") == sb.DEFAULT_CAPACITY_COOLDOWN_S

    def test_latch_expires_rather_than_persisting(self, sb, monkeypatch):
        """TTL, not a permanent latch: a recovered endpoint must become
        usable again without the user deleting a file."""
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_COOLDOWN", "60")
        base = sb._now()
        monkeypatch.setattr(sb, "_now", lambda: base)
        sb.trip_backend("cli", "rate_limit", "429")
        assert sb.breaker_state("cli") is not None
        monkeypatch.setattr(sb, "_now", lambda: base + 59)
        assert sb.breaker_state("cli") is not None
        monkeypatch.setattr(sb, "_now", lambda: base + 61)
        assert sb.breaker_state("cli") is None


# ══════════════════════════════════════════════════════════════════════
# Knobs (R24: a reader AND a test that setting it changes something)
# ══════════════════════════════════════════════════════════════════════
class TestKnobs:
    @pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
    def test_kill_switch_disables_the_breaker(self, sb, monkeypatch, value):
        monkeypatch.setenv("VCO_SUMMARY_BREAKER", value)
        assert sb.breaker_enabled() is False
        assert sb.trip_backend("cli", "rate_limit", "429") is False
        assert sb.breaker_state("cli") is None
        assert not sb._breaker_path().exists()

    def test_cooldown_knob_changes_the_window(self, sb, monkeypatch):
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_COOLDOWN", "42")
        assert sb.cooldown_for("rate_limit") == 42.0
        base = sb._now()
        monkeypatch.setattr(sb, "_now", lambda: base)
        sb.trip_backend("cli", "rate_limit", "429")
        assert sb.breaker_state("cli")["remaining"] in (41, 42)

    def test_capacity_cooldown_knob_changes_the_window(self, sb, monkeypatch):
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_CAPACITY_COOLDOWN", "7")
        assert sb.cooldown_for("capacity") == 7.0

    def test_capacity_strikes_knob_changes_the_threshold(self, sb, monkeypatch):
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_CAPACITY_STRIKES", "1")
        assert sb.capacity_strikes() == 1
        assert sb.trip_backend("cli", "capacity", "529") is True

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "-5", "0"])
    def test_unparseable_cooldown_falls_back_to_the_default(self, sb,
                                                            monkeypatch, raw):
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_COOLDOWN", raw)
        expected = 0.0 if raw == "0" else sb.DEFAULT_BREAKER_COOLDOWN_S
        assert sb.cooldown_for("rate_limit") == expected


# ══════════════════════════════════════════════════════════════════════
# The cross-process latch — the KG generator runs ONE PROCESS PER NODE
# ══════════════════════════════════════════════════════════════════════
class TestCrossProcessLatch:
    def test_a_second_process_sees_the_first_process_trip(self, sb, state_dir):
        sb.trip_backend("cli", "rate_limit", "usage limit")
        assert (state_dir / sb.BREAKER_FILENAME).is_file()

        second = _load("_wpq_sb_proc2", _SB_PATH)   # a fresh "process"
        second.reset_backend_cache()                # no in-memory carry-over
        assert second._BREAKER_MEM == {}
        assert second.breaker_state("cli") is not None

    def test_a_demoted_cli_never_pays_the_smoke_test_again(self, sb,
                                                           monkeypatch,
                                                           state_dir):
        """The 20 s probe once per node is most of what the breaker exists
        to stop — selection must skip the tier BEFORE probing it."""
        sb.trip_backend("cli", "rate_limit", "usage limit")
        second = _load("_wpq_sb_proc3", _SB_PATH)
        second.reset_backend_cache()
        lines: list[str] = []
        second.set_logger(lines.append)
        probed = []
        monkeypatch.setattr(second, "cli_available",
                            lambda: (probed.append(1), True)[1])
        monkeypatch.setattr(second, "ollama_available", lambda: True)
        assert second.select_backend() == "ollama"
        assert probed == [], "cli_available() ran despite an open breaker"
        assert any("breaker open" in line for line in lines)

    def test_latch_lives_under_the_vct_state_dir_not_the_project(self, sb,
                                                                 state_dir):
        sb.trip_backend("cli", "rate_limit", "429")
        assert sb._breaker_path().parent == state_dir
        payload = json.loads((state_dir / sb.BREAKER_FILENAME).read_text())
        assert payload["cli"]["reason"] == "rate_limit"

    def test_save_soft_fails_on_an_unwritable_state_dir(self, sb, monkeypatch,
                                                        tmp_path):
        """An unwritable state dir must not break a summary run; the
        in-process latch still holds."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("VCT_STATE_DIR", str(blocker / "state"))
        assert sb.trip_backend("cli", "rate_limit", "429") is True
        assert "cli" in sb._BREAKER_MEM


# ══════════════════════════════════════════════════════════════════════
# Capacity STRIKES cross the process boundary too (v0.2.92 BLOCKER-2)
# ══════════════════════════════════════════════════════════════════════
class TestCapacityStrikesCrossProcess:
    """The strike counter has to survive the process boundary, or the
    ``capacity`` arm of the breaker cannot fire at all.

    ``launcher/src-tauri/src/commands/kg_summary.rs`` spawns
    ``generate-kg-summary.py <file>`` ONCE PER NODE. An in-process strike
    dict is therefore re-zeroed before the second 529 ever arrives: during
    an Opus 529 window every node is strike #1, the latch is never
    written, and all 117 nodes pay the full timeout against a tier that is
    down — the exact waste the breaker exists to stop, for the most common
    transient reason. ``rate_limit``/``auth`` (first-occurrence) always
    worked, which is why the one-process test below it could not see this.
    """

    def test_strikes_accumulate_across_separate_module_loads(self, sb, state_dir):
        assert sb.trip_backend("cli", "capacity", "529") is False

        second = _load("_wpq_sb_cap2", _SB_PATH)      # node 2 = a new process
        second.reset_backend_cache()
        assert second._BREAKER_MEM == {}, "in-process carry-over would fake a pass"
        assert second.trip_backend("cli", "capacity", "529") is False

        third = _load("_wpq_sb_cap3", _SB_PATH)       # node 3
        third.reset_backend_cache()
        assert third._BREAKER_MEM == {}
        assert third.trip_backend("cli", "capacity", "529") is True, (
            "the third consecutive 529 did not demote the tier — the strike "
            "count did not survive the process boundary (BLOCKER-2)"
        )

        fourth = _load("_wpq_sb_cap4", _SB_PATH)      # node 4 sees the latch
        fourth.reset_backend_cache()
        record = fourth.breaker_state("cli")
        assert record is not None and record["reason"] == "capacity"

    def test_a_pending_strike_is_not_itself_an_open_breaker(self, sb, state_dir):
        """Persisting the count must not demote the tier early — below the
        threshold the tier stays fully usable, in this process and the next."""
        sb.trip_backend("cli", "capacity", "529")
        assert sb.breaker_state("cli") is None
        payload = json.loads((state_dir / sb.BREAKER_FILENAME).read_text())
        assert payload["cli"]["strikes"] == 1
        second = _load("_wpq_sb_cap5", _SB_PATH)
        second.reset_backend_cache()
        assert second.breaker_state("cli") is None

    def test_a_success_in_a_later_process_resets_the_count(self, sb, state_dir):
        sb.trip_backend("cli", "capacity", "529")
        sb.trip_backend("cli", "capacity", "529")

        second = _load("_wpq_sb_cap6", _SB_PATH)
        second.reset_backend_cache()
        second.clear_backend_trip("cli")              # this node's call succeeded

        third = _load("_wpq_sb_cap7", _SB_PATH)
        third.reset_backend_cache()
        assert third.trip_backend("cli", "capacity", "529") is False, (
            "strikes must be CONSECUTIVE — a success between them resets"
        )
        assert third.breaker_state("cli") is None

    def test_a_strike_never_clears_a_latch_that_is_already_open(self, sb):
        """The strike record carries ``until: 0``. If it could overwrite a
        live latch, counting a strike would RE-OPEN a demoted tier."""
        sb.trip_backend("cli", "rate_limit", "usage limit")
        assert sb.trip_backend("cli", "capacity", "529") is False
        assert sb.breaker_state("cli") is not None, (
            "a below-threshold capacity strike cleared a live latch"
        )

    def test_the_strike_knob_governs_the_cross_process_count(
        self, sb, monkeypatch, state_dir,
    ):
        monkeypatch.setenv("VCO_SUMMARY_BREAKER_CAPACITY_STRIKES", "2")
        assert sb.trip_backend("cli", "capacity", "529") is False
        second = _load("_wpq_sb_cap8", _SB_PATH)
        second.reset_backend_cache()
        assert second.trip_backend("cli", "capacity", "529") is True


# ══════════════════════════════════════════════════════════════════════
# call_llm — the ladder actually descends
# ══════════════════════════════════════════════════════════════════════
class TestLadderDescends:
    def test_usage_limit_on_cli_falls_through_to_ollama(self, sb, monkeypatch):
        _pin_probes(monkeypatch, sb, cli=True, ollama=True)
        monkeypatch.setattr(sb, "call_cli", lambda p: (_ for _ in ()).throw(
            sb.BackendUnavailable("cli", "rate_limit", "usage limit",
                                  "claude CLI failed: usage limit reached")))
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: "Resolves the project config from the hub.")

        out = sb.call_llm("summarize this")

        assert out == "Resolves the project config from the hub."
        assert sb.breaker_state("cli") is not None
        assert sb._BACKEND_CACHE["choice"] == "ollama"

    def test_the_tier_change_is_observable(self, sb, monkeypatch):
        _pin_probes(monkeypatch, sb, cli=True, ollama=True)
        monkeypatch.setattr(sb, "call_cli", lambda p: (_ for _ in ()).throw(
            sb.BackendUnavailable("cli", "rate_limit", "usage limit", "boom")))
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: "Parses the manifest and returns actions.")
        sb.call_llm("x", label="KG-summary")
        joined = "\n".join(sb.log_lines)
        assert "demoted" in joined and "rate_limit" in joined
        assert "backend: ollama" in joined, (
            "the tier that actually answered must be named in the log"
        )

    def test_a_second_call_does_not_retry_the_demoted_tier(self, sb, monkeypatch):
        _pin_probes(monkeypatch, sb, cli=True, ollama=True)
        cli_calls = []
        monkeypatch.setattr(sb, "call_cli", lambda p: (
            cli_calls.append(p),
            (_ for _ in ()).throw(sb.BackendUnavailable(
                "cli", "rate_limit", "usage limit", "boom")),
        )[1])
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: "Writes the sidecar atomically.")
        for _ in range(5):
            sb.call_llm("x")
        assert len(cli_calls) == 1, (
            "the CLI was re-attempted after the breaker opened — this is the "
            "426-session defect"
        )

    def test_unclassified_failure_raises_without_demoting(self, sb, monkeypatch):
        """A breaker that trips on everything falls back when it should
        retry — the opposite defect."""
        _pin_probes(monkeypatch, sb, cli=True, ollama=True)
        monkeypatch.setattr(sb, "call_cli", lambda p: (_ for _ in ()).throw(
            RuntimeError("claude CLI failed: unexpected json shape")))
        called = []
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: (called.append(p), "x")[1])
        with pytest.raises(RuntimeError):
            sb.call_llm("x")
        assert called == []
        assert sb.breaker_state("cli") is None

    def test_a_forced_backend_is_never_silently_substituted(self, sb, monkeypatch):
        monkeypatch.setenv("KG_SUMMARY_BACKEND", "cli")
        monkeypatch.setattr(sb, "call_cli", lambda p: (_ for _ in ()).throw(
            sb.BackendUnavailable("cli", "rate_limit", "usage limit", "boom")))
        served = []
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: (served.append(p), "x")[1])
        with pytest.raises(sb.BackendUnavailable):
            sb.call_llm("x")
        assert served == [], "a forced tier must not be swapped for another model"
        # It still LATCHES, so the next node fails fast instead of spawning.
        assert sb.breaker_state("cli") is not None

    def test_forced_and_demoted_stops_spawning_the_subprocess(self, sb, monkeypatch):
        monkeypatch.setenv("KG_SUMMARY_BACKEND", "cli")
        sb.trip_backend("cli", "rate_limit", "usage limit")
        spawned = []
        monkeypatch.setattr(sb, "call_cli",
                            lambda p: (spawned.append(p), "x")[1])
        with pytest.raises(sb.BackendUnavailable):
            sb.call_llm("x")
        assert spawned == []

    def test_http_429_from_an_api_tier_is_classified_and_demotes(self, sb,
                                                                 monkeypatch):
        _pin_probes(monkeypatch, sb, ollama=True, api=True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        def _boom(prompt, **kw):
            raise urllib.error.HTTPError(
                "https://api.anthropic.com/v1/messages", 429,
                "Too Many Requests", Message(), None)

        monkeypatch.setattr(sb, "call_ollama", _boom)
        monkeypatch.setattr(sb, "call_api",
                            lambda p: "Returns the resolved collection name.")
        out = sb.call_llm("x")
        assert out == "Returns the resolved collection name."
        assert sb.breaker_state("ollama")["reason"] == "rate_limit"

    def test_no_backend_left_raises_the_marker_the_launcher_reads(self, sb,
                                                                  monkeypatch):
        _pin_probes(monkeypatch, sb)
        with pytest.raises(RuntimeError, match="no backend available"):
            sb.call_llm("x")

    def test_skip_line_keeps_the_launcher_marker_and_tells_the_truth(
        self, sb, monkeypatch,
    ):
        """"Install the claude CLI" is the WRONG advice for a user whose CLI
        is installed and merely rate-limited — but the launcher's
        NO_BACKEND_MARKER substring must survive either way."""
        _pin_probes(monkeypatch, sb, cli=True)
        sb.trip_backend("cli", "rate_limit", "usage limit")
        assert sb.select_backend() == "skip"
        joined = "\n".join(sb.log_lines)
        assert "no backend available" in joined, "launcher marker lost"
        assert "cooling down" in joined
        assert "no claude CLI" not in joined

    def test_skip_line_is_unchanged_when_nothing_is_cooling_down(
        self, sb, monkeypatch,
    ):
        _pin_probes(monkeypatch, sb)
        assert sb.select_backend() == "skip"
        joined = "\n".join(sb.log_lines)
        assert "no backend available (no claude CLI, no Ollama at" in joined

    def test_a_success_clears_the_persisted_latch(self, sb, monkeypatch):
        _pin_probes(monkeypatch, sb, ollama=True)
        sb.trip_backend("ollama", "rate_limit", "usage limit")
        sb._BREAKER_MEM.clear()          # simulate a later process
        base = sb._now()
        monkeypatch.setattr(sb, "_now", lambda: base + 10_000)  # cooled down
        monkeypatch.setattr(sb, "call_ollama",
                            lambda p, **kw: "Emits one progress event per node.")
        sb.call_llm("x")
        assert json.loads(sb._breaker_path().read_text()) == {}


# ══════════════════════════════════════════════════════════════════════
# call_cli — prompt on stdin (the Windows argv-truncation root cause)
# ══════════════════════════════════════════════════════════════════════
class TestCallCliTransport:
    @pytest.fixture()
    def spy(self, sb, monkeypatch):
        monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/claude")
        seen: dict = {}

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return types.SimpleNamespace(
                returncode=seen.get("rc", 0),
                stdout=seen.get("stdout", "Resolves and caches the KG collection name."),
                stderr=seen.get("stderr", ""),
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        return sb, seen

    def test_prompt_travels_on_stdin_not_argv(self, spy):
        sb, seen = spy
        body = "Title: X\n\nContent:\n- a & b | c ^ d %PATH% > e"
        sb.call_cli(body)
        argv = seen["argv"]
        assert argv == ["/usr/bin/claude", "-p", "--model", "haiku",
                        "--max-turns", "1", "--no-session-persistence"]
        joined_argv = " ".join(argv)
        assert body not in joined_argv
        assert sb.SYSTEM_PROMPT not in joined_argv, (
            "the prompt is back in argv — on Windows cmd.exe re-parses it and "
            "truncates at the first newline (the non-answer root cause)"
        )
        assert seen["kwargs"]["input"] == sb.SYSTEM_PROMPT + "\n\n" + body

    def test_call_cli_argv_carries_no_session_persistence(self, spy):
        """Every one-shot summary call must opt out of transcript
        persistence: without the flag each call writes a session into the
        user's chat picker (943 machine-generated transcripts / 1.05 GB
        measured on one install). Verified on Claude CLI 2.1.258+;
        post-git-commit-kg-sync.{sh,ps1} already pass the same flag."""
        sb, seen = spy
        sb.call_cli("x")
        assert "--no-session-persistence" in seen["argv"]

    def test_cli_probe_argv_carries_no_session_persistence(self, sb,
                                                           monkeypatch):
        """The "say ok" smoke-test is itself a one-shot claude -p call —
        without the flag it persists one transcript per probe (~35 "say ok"
        sessions measured on one install). cli_available() caches its
        result, so reset the cache before spying on the probe."""
        sb.reset_backend_cache()
        monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/claude")
        seen: dict = {}

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert sb.cli_available() is True
        assert "--no-session-persistence" in seen["argv"]

    def test_no_argv_element_contains_a_newline(self, spy):
        """The precise Windows failure: cmd.exe ends the command at a
        literal newline in a batch-file argument."""
        sb, seen = spy
        sb.call_cli("line one\nline two\nline three")
        assert not any("\n" in arg for arg in seen["argv"])

    def test_nonzero_exit_with_a_usage_limit_stderr_is_classified(self, spy):
        sb, seen = spy
        seen["rc"] = 1
        seen["stderr"] = "Claude AI usage limit reached. Resets at 3pm."
        with pytest.raises(sb.BackendUnavailable) as excinfo:
            sb.call_cli("x")
        assert excinfo.value.reason == "rate_limit"

    def test_exit_zero_notice_is_a_tier_failure_not_a_summary(self, spy):
        """Some backends print the notice and exit 0. Caching it poisons the
        sidecar AND hides the outage."""
        sb, seen = spy
        seen["stdout"] = "Claude AI usage limit reached"
        with pytest.raises(sb.BackendUnavailable) as excinfo:
            sb.call_cli("x")
        assert excinfo.value.reason == "rate_limit"

    def test_a_long_summary_mentioning_rate_limits_is_still_returned(self, spy):
        """The exit-0 re-read must not eat a genuine summary about the
        topic — only short notices are eligible."""
        sb, seen = spy
        seen["stdout"] = (
            "Documents the retry policy: the client backs off when the "
            "server returns a rate limit response, and the usage limit "
            "counter resets hourly. " + "Details follow. " * 20
        )
        assert "rate limit" in sb.call_cli("x")

    @pytest.mark.parametrize("notice", [
        "Claude AI usage limit reached. Your limit will reset at 3pm.",
        "Invalid API key · Please run /login",
        "API Error: 529 Overloaded",
    ])
    def test_exit_zero_notices_are_caught(self, spy, notice):
        sb, seen = spy
        seen["stdout"] = notice
        with pytest.raises(sb.BackendUnavailable):
            sb.call_cli("x")

    @pytest.mark.parametrize("summary", [
        "Retries on 429 responses with exponential backoff.",
        "Times out after 500 ms and returns None to the caller.",
        "Maps a 503 from the upstream service onto a typed error.",
        "Raises when the server reports a rate limit, so the caller can wait.",
        "Error handling: unauthorized callers get a typed refusal.",
    ])
    def test_a_short_summary_about_errors_is_not_read_as_an_outage(
        self, spy, summary,
    ):
        """The reply is CONTENT the model was asked to write about something.
        A bare substring scan would demote a healthy tier on the strength of
        the code it just summarised."""
        sb, seen = spy
        seen["stdout"] = summary
        assert sb.call_cli("x") == summary
        assert sb.breaker_state("cli") is None

    def test_timeout_is_a_capacity_condition(self, sb, monkeypatch):
        monkeypatch.setattr(sb.shutil, "which", lambda name: "/usr/bin/claude")

        def _timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 180)

        monkeypatch.setattr(subprocess, "run", _timeout)
        with pytest.raises(sb.BackendUnavailable) as excinfo:
            sb.call_cli("x")
        assert excinfo.value.reason == "capacity"


# ══════════════════════════════════════════════════════════════════════
# Non-answer detection
# ══════════════════════════════════════════════════════════════════════
class TestNonAnswerPredicate:
    @pytest.mark.parametrize("text", [
        "Ready. What do you need summarized?",
        "Ready! What would you like me to summarize?",
        "",
        "   \n  ",
        None,
        "ok",
        "N/A",
        "I cannot summarize this content.",
        "I'm unable to help with that.",
        "Sorry, I don't have the content you're referring to.",
        "Please provide the content you'd like summarized.",
        "As an AI, I can only work with what you give me.",
        "I don't see any content to summarize.",
        "Error: no input received",
    ])
    def test_non_answers_are_rejected(self, sb, text):
        assert sb.is_non_answer(text) is True, text

    @pytest.mark.parametrize("text", [
        "Resolves the per-project KG collection name via vct-hub, falling "
        "back to env when the hub is unreachable.",
        "A content-hash gate that skips regeneration when the source file is "
        "unchanged; the hash is sha256[:16] of the full file text.",
        "Ready-state probe for Weaviate: polls /v1/.well-known/ready with a "
        "bounded timeout and raises TimeoutError instead of hanging.",
        "The caller cannot assume the transcript is flushed; the reader "
        "treats a missing entry as an empty context.",
        "Parses YAML frontmatter.",
    ])
    def test_real_summaries_are_kept(self, sb, text):
        assert sb.is_non_answer(text) is False, text

    def test_a_refusal_phrase_mid_text_is_not_a_non_answer(self, sb):
        """Prefix-anchored on purpose: a summary may DESCRIBE a refusal."""
        assert sb.is_non_answer(
            "Documents the guard that answers 'I cannot verify this' when the "
            "hub is unreachable, and why fail-open was rejected."
        ) is False

    def test_call_llm_refuses_to_return_a_non_answer(self, sb, monkeypatch):
        _pin_probes(monkeypatch, sb, cli=True)
        monkeypatch.setattr(sb, "call_cli",
                            lambda p: "Ready. What do you need summarized?")
        with pytest.raises(sb.NonAnswerResponse):
            sb.call_llm("x")

    def test_a_non_answer_does_not_demote_the_tier(self, sb, monkeypatch):
        """Content condition, not a tier condition — demoting here would
        fall back when the tier is perfectly healthy."""
        _pin_probes(monkeypatch, sb, cli=True, ollama=True)
        monkeypatch.setattr(sb, "call_cli", lambda p: "I cannot do that.")
        with pytest.raises(sb.NonAnswerResponse):
            sb.call_llm("x")
        assert sb.breaker_state("cli") is None

    def test_the_non_answer_error_never_quotes_the_model_output(self, sb,
                                                                monkeypatch):
        """The callers LOG this message (``generate-kg-summary.py`` writes it
        to the project log), and model output on this path quotes node
        content — so the message reports the reply's SHAPE, never its text.
        Same rule as the breaker's ``signal`` field."""
        _pin_probes(monkeypatch, sb, cli=True)
        monkeypatch.setattr(
            sb, "call_cli", lambda p: "I cannot summarize SECRET-NODE-BODY.")
        with pytest.raises(sb.NonAnswerResponse) as excinfo:
            sb.call_llm("x")
        message = str(excinfo.value)
        assert "SECRET-NODE-BODY" not in message
        assert "cli" in message and "chars" in message


# ══════════════════════════════════════════════════════════════════════
# KG generator — poisoned rows heal; healthy rows still skip
# ══════════════════════════════════════════════════════════════════════
def _kg_module(monkeypatch, root: Path):
    monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
    mod = _load("_wpq_kg", _KG_PATH)
    mod._sb.reset_backend_cache()
    mod._sb.reset_breaker(persisted=True)
    monkeypatch.setattr(mod, "select_backend", lambda: "cli")
    monkeypatch.setattr(
        mod, "get_chunks_from_weaviate", lambda title, file_path="": [])
    return mod


def _seed_node(root: Path) -> Path:
    node = root / "knowledge" / "concepts" / "thing.md"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text(
        "---\ntitle: Thing\n---\n\nThe thing does the thing, carefully.\n",
        encoding="utf-8",
    )
    return node


class TestKgGeneratorHealsPoisonedRows:
    def test_healthy_entry_with_matching_hash_still_skips(
        self, state_dir, monkeypatch, tmp_path, capsys,
    ):
        root = tmp_path / "proj"
        root.mkdir()
        mod = _kg_module(monkeypatch, root)
        node = _seed_node(root)
        c_hash = mod.content_hash(node.read_text(encoding="utf-8"))
        rel = str(node.relative_to(root))
        mod.save_formats({rel: {
            "title": "Thing",
            "description": "Describes the thing and how it is used in practice.",
            "summary": "The thing does the thing, with a bounded retry.",
            "content_hash": c_hash,
        }})
        called = []
        monkeypatch.setattr(mod, "generate_description",
                            lambda t, b: (called.append(1), "x")[1])
        monkeypatch.setattr(sys, "argv", ["gen", str(node)])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        assert called == [], "the hash gate regressed — this is the 34 s/node cost"
        assert "unchanged (hash match), skipping" in capsys.readouterr().out

    def test_poisoned_entry_with_matching_hash_regenerates(
        self, state_dir, monkeypatch, tmp_path,
    ):
        root = tmp_path / "proj"
        root.mkdir()
        mod = _kg_module(monkeypatch, root)
        node = _seed_node(root)
        c_hash = mod.content_hash(node.read_text(encoding="utf-8"))
        rel = str(node.relative_to(root))
        mod.save_formats({rel: {
            "title": "Thing",
            "description": "Ready. What do you need summarized?",
            "summary": "Ready. What do you need summarized?",
            "content_hash": c_hash,
        }})
        monkeypatch.setattr(
            mod, "generate_description",
            lambda t, b: "Describes the thing and its bounded retry policy.")
        monkeypatch.setattr(
            mod, "generate_summary",
            lambda t, b: "The thing retries twice and then gives up loudly.")
        monkeypatch.setattr(sys, "argv", ["gen", str(node)])
        mod.main()                       # completes: no early skip-exit
        stored = mod.load_formats()[rel]
        assert "bounded retry policy" in stored["description"], (
            "a row that never held a valid summary stayed frozen by the hash gate"
        )

    def test_a_non_answer_is_never_written_to_the_sidecar(
        self, state_dir, monkeypatch, tmp_path,
    ):
        root = tmp_path / "proj"
        root.mkdir()
        mod = _kg_module(monkeypatch, root)
        node = _seed_node(root)
        monkeypatch.setattr(mod, "generate_description", lambda t, b: (
            (_ for _ in ()).throw(mod._sb.NonAnswerResponse("non-answer"))))
        monkeypatch.setattr(sys, "argv", ["gen", str(node)])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1, "a non-answer must fail loudly, not cache"
        assert mod.load_formats() == {}

    def test_the_sidecar_records_the_tier_that_actually_answered(
        self, state_dir, monkeypatch, tmp_path,
    ):
        """The user-visible half of observability: after a demotion the
        entry must say `ollama`, not the tier that was picked first."""
        root = tmp_path / "proj"
        root.mkdir()
        monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
        mod = _load("_wpq_kg_backend", _KG_PATH)
        mod._sb.reset_backend_cache()
        mod._sb.reset_breaker(persisted=True)
        monkeypatch.setattr(
        mod, "get_chunks_from_weaviate", lambda title, file_path="": [])
        _pin_probes(monkeypatch, mod._sb, cli=True, ollama=True)
        monkeypatch.setattr(mod._sb, "call_cli", lambda p: (_ for _ in ()).throw(
            mod._sb.BackendUnavailable("cli", "rate_limit", "usage limit",
                                       "usage limit reached")))
        monkeypatch.setattr(
            mod._sb, "call_ollama",
            lambda p, **kw: "Describes the thing and its bounded retry policy.")
        node = _seed_node(root)
        monkeypatch.setattr(sys, "argv", ["gen", str(node)])
        mod.main()
        entry = mod.load_formats()[str(node.relative_to(root))]
        assert entry["backend"] == "ollama", (
            "a summary produced by Ollama was recorded as one produced by "
            "the tier that was demoted"
        )

    def test_stored_entry_validity_helper(self, state_dir, monkeypatch, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        mod = _kg_module(monkeypatch, root)
        good = {"description": "Describes the resolver chain and its fallbacks.",
                "summary": "Resolves via hub, then env, then the default."}
        assert mod.stored_entry_is_usable(good) is True
        assert mod.stored_entry_is_usable({**good, "summary": ""}) is False
        assert mod.stored_entry_is_usable(
            {**good, "description": "I cannot summarize this."}) is False
        assert mod.stored_entry_is_usable(
            {**good, "chunk_summaries": {"1": "Ready. What do you need?"}}
        ) is False
        assert mod.stored_entry_is_usable(
            {**good, "chunk_summaries": {"1": "Covers the retry policy only."}}
        ) is True


# ══════════════════════════════════════════════════════════════════════
# Code generator — poisoned rows are stale; an exhausted ladder stops
# ══════════════════════════════════════════════════════════════════════
def _code_module(monkeypatch, root: Path):
    monkeypatch.setenv("KG_PROJECT_ROOT", str(root))
    mod = _load("_wpq_code", _CODE_PATH)
    mod._sb.reset_backend_cache()
    mod._sb.reset_breaker(persisted=True)
    return mod


class TestCodeGeneratorStaleness:
    def test_poisoned_entry_is_stale_despite_a_matching_hash(
        self, state_dir, monkeypatch, tmp_path,
    ):
        mod = _code_module(monkeypatch, tmp_path)
        poisoned = {"one_liner": "Ready. What do you need summarized?",
                    "summary": "", "content_hash": "h1"}
        assert mod.needs_generation(poisoned, "h1", False) is True

    def test_healthy_entry_with_a_matching_hash_is_not_regenerated(
        self, state_dir, monkeypatch, tmp_path,
    ):
        mod = _code_module(monkeypatch, tmp_path)
        healthy = {"one_liner": "Resolves the collection prefix for a project.",
                   "summary": "Looks the prefix up via the endorsed sanitizer.",
                   "content_hash": "h1"}
        assert mod.needs_generation(healthy, "h1", False) is False

    def test_an_empty_summary_on_a_trivial_body_is_not_poisoned(
        self, state_dir, monkeypatch, tmp_path,
    ):
        """Trivial bodies get a one_liner only — an empty summary there is
        by design, not a non-answer."""
        mod = _code_module(monkeypatch, tmp_path)
        trivial = {"one_liner": "Returns the module-level default timeout.",
                   "summary": "", "content_hash": "h1"}
        assert mod.entry_is_poisoned(trivial) is False
        assert mod.needs_generation(trivial, "h1", False) is False

    def test_run_stops_early_when_every_tier_is_cooling_down(
        self, state_dir, monkeypatch, tmp_path,
    ):
        """The decision that gates work, not just the happy path: with no
        tier left, walking the rest of the worklist is what produced
        hundreds of doomed spawns."""
        mod = _code_module(monkeypatch, tmp_path)
        rows = [{"full_name": f"m.f{i}", "file_path": "src/m.py",
                 "content_hash": "h1", "n_callers": 10 - i,
                 "total_chunks": 1, "_body": "x" * 400,
                 "signature": "def f()", "doc": ""} for i in range(8)]
        monkeypatch.setattr(mod, "_collection_prefix", lambda name: "Proj")
        monkeypatch.setattr(mod, "_connect_weaviate",
                            lambda: types.SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(
            mod, "_iter_canonical_rows",
            lambda client, prefix, base: rows if base == "CodeFunction" else [])
        attempts: list = []

        def _capped(prompt):
            # The REAL ladder runs above this, so the failure classifies,
            # demotes the tier, and the generator sees an exhausted ladder.
            attempts.append(prompt)
            raise mod._sb.BackendUnavailable(
                "cli", "rate_limit", "usage limit", "usage limit reached")

        monkeypatch.setattr(mod._sb, "call_cli", _capped)
        # Only the CLI tier exists, so demoting it exhausts the ladder.
        monkeypatch.setattr(mod._sb, "cli_available", lambda: True)
        monkeypatch.setattr(mod._sb, "ollama_available", lambda: False)
        monkeypatch.setattr(mod._sb, "openai_available", lambda: False)
        monkeypatch.setattr(mod._sb, "api_available", lambda: False)

        assert mod.run("AnyProj", project_root=tmp_path, max_per_run=8,
                       force=False) == 0
        assert len(attempts) == 1, (
            f"walked the worklist after the ladder was exhausted "
            f"({len(attempts)} spawns for 8 rows)"
        )
        assert mod._sb.breaker_state("cli") is not None

    def test_a_poisoned_summary_is_detected_when_non_empty(
        self, state_dir, monkeypatch, tmp_path,
    ):
        mod = _code_module(monkeypatch, tmp_path)
        entry = {"one_liner": "Returns the module-level default timeout.",
                 "summary": "I cannot analyze this code.", "content_hash": "h1"}
        assert mod.entry_is_poisoned(entry) is True


# ══════════════════════════════════════════════════════════════════════
# m13 — the `_vct_state_dir` mirror is PINNED
# ══════════════════════════════════════════════════════════════════════
class TestVctStateDirMirrorParity:
    """``summary_backends._vct_state_dir`` is a DELIBERATE tier-C copy of
    ``vco_lib.paths.vct_root_dir``: this module ships into user projects as
    ``.claude/scripts/`` where ``vco_lib`` is not importable, so it is
    stdlib-only by design. What was missing is the pin — the file's OTHER
    declared copy (``_save_breaker_file`` ← ``vco_lib.atomic``) is already
    locked by ``tests/test_v0292_atomic_one_home.py``, and a declared mirror
    without a parity test is how mirrors drift. This pins the copy; it does
    not ask for the copy to be removed.
    """

    @pytest.mark.parametrize("raw", [
        None, "", "   ", "/tmp/vct-parity-probe", "  /tmp/vct-parity-probe  ",
    ])
    def test_resolves_identically_to_the_home(self, sb, monkeypatch, raw):
        from vco_lib.paths import vct_root_dir
        if raw is None:
            monkeypatch.delenv("VCT_STATE_DIR", raising=False)
        else:
            monkeypatch.setenv("VCT_STATE_DIR", raw)
        assert sb._vct_state_dir() == vct_root_dir()

    def test_the_copy_names_the_home_it_mirrors(self):
        assert "vco_lib.paths.vct_root_dir" in _SB_PATH.read_text(
            encoding="utf-8"), (
            "the mirror must NAME its home in-file so the next editor "
            "finds it (same rule as the atomic-write copy)"
        )
