# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""WP-E (v0.2.92): hook query enrichment + query-budget SSOT.

Covers ``vco_lib.transcript_context`` (the shared JSONL transcript reader)
and ``vco_lib.query_enrichment.build_query`` (the enrichment/chunking
decision + composition), per PLAN §5.2's fixed contract and the acceptance
criteria in PLAN-v0292-WAVE3-2026-09-03.md's WP-E section:

  * short trigger + fixture transcript -> enriched, tool payloads excluded
  * long (over-budget) trigger -> unchanged, never enriched
  * lagging transcript (newest turn not yet flushed) -> ``lagging=True``,
    the PENDING prompt is the one used, and the previous complete turn
    supplies only the assistant-text fallback (never the stale prompt)
  * budget resolution uses the MIN across a multi-model list
  * unknown model name -> conservative (skip enrichment), not "assume large"
  * the budget is the EMBEDDING model's ``num_ctx``, not the chunking
    preset's max (R29), and the conservative branch keeps the safety margin
  * ``VCO_QUERY_ENRICH=off`` -> byte-identical to a disabled call
  * threshold / share env knobs are each independently observable, and
    ``share`` is clamped to [SHARE_MIN, SHARE_MAX]
  * no transcript fixture string ever appears in a raised exception message
    (dynamic: a forced mid-composition failure; plus a static check that
    neither module has a print/logger/stderr surface at all)
  * every ``build_query`` decision emits ONE observation row (counts +
    digest, never text) so "did enrichment ever fire" is answerable
  * every hook call site forwards ``prompt_id``/``transcript_path`` to BOTH
    the code-graph and the KG producer
  * cache-key differentiation across ``prompt_id`` is QUERY-CACHE's job
    (``templates/hooks/_lib/query-cache.sh``), NOT this module's — not
    re-tested here, see that file's own test coverage
"""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import pytest

from vco_lib.query_enrichment import (
    DEFAULT_SHARE,
    DEFAULT_SHORT_THRESHOLD_TOKENS,
    ENV_ENABLE,
    ENV_SHARE,
    ENV_SHORT_THRESHOLD,
    EnrichedQuery,
    build_query,
)
from vco_lib.transcript_context import TurnContext, iter_turns, last_turn_context

_QWEN3 = "qwen3-embedding:0.6b"
_CODESAGE = "codesage/codesage-large-v2"


def _write_transcript(tmp_path, entries):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    return str(p)


def _user_entry(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result_entry():
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": "irrelevant"}],
        },
    }


def _assistant_entry(uuid, text=None, thinking=None, tool_use=None):
    blocks = []
    if thinking is not None:
        blocks.append({"type": "thinking", "thinking": thinking})
    if text is not None:
        blocks.append({"type": "text", "text": text})
    if tool_use is not None:
        blocks.append({"type": "tool_use", "name": tool_use, "input": {"secret": "SHOULD_NEVER_SURFACE"}})
    return {"type": "assistant", "uuid": uuid, "message": {"role": "assistant", "content": blocks}}


def _sidechain_entry(uuid, text):
    e = _assistant_entry(uuid, text=text)
    e["isSidechain"] = True
    return e


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # These tests must not be influenced by whatever the outer shell/session
    # happens to have exported for these knobs.
    for name in (ENV_ENABLE, ENV_SHORT_THRESHOLD, ENV_SHARE):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# transcript_context — reader semantics.
# ---------------------------------------------------------------------------


class TestTranscriptContextReader:
    def test_no_path_returns_empty_lagging_result(self):
        turn = last_turn_context(None)
        assert isinstance(turn, TurnContext)
        assert turn.lagging is True
        assert turn.user_prompt == ""
        assert turn.assistant_text == ""
        assert turn.assistant_thinking == ""

    def test_missing_file_returns_empty_lagging_result(self, tmp_path):
        turn = last_turn_context(str(tmp_path / "does_not_exist.jsonl"))
        assert turn.lagging is True

    def test_recovers_text_and_thinking_excludes_tool_blocks(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [
                _user_entry("what does the retry loop do"),
                _assistant_entry(
                    "u1",
                    text="the retry loop backs off exponentially",
                    thinking="I should check the backoff constant first",
                    tool_use="hybrid_search",
                ),
            ],
        )
        turn = last_turn_context(path)
        assert turn.lagging is False
        assert turn.user_prompt == "what does the retry loop do"
        assert turn.assistant_text == "the retry loop backs off exponentially"
        assert turn.assistant_thinking == "I should check the backoff constant first"
        # tool_use input must never leak into either text field.
        assert "SHOULD_NEVER_SURFACE" not in turn.assistant_text
        assert "SHOULD_NEVER_SURFACE" not in turn.assistant_thinking
        assert "hybrid_search" not in turn.assistant_text

    def test_tool_result_user_entry_is_not_mistaken_for_a_prompt(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [
                _user_entry("real prompt"),
                _assistant_entry("u1", text="ack", tool_use="Bash"),
                _tool_result_entry(),
                _assistant_entry("u2", text="final answer"),
            ],
        )
        turn = last_turn_context(path)
        # The tool_result-only "user" entry must not overwrite the real
        # prompt that preceded it — no new genuine user prompt occurred
        # between the two assistant entries, so they are correctly one
        # logical turn (Claude Code emits a separate JSONL "assistant"
        # entry per tool round-trip within a single turn) and their text
        # is concatenated in order.
        assert turn.user_prompt == "real prompt"
        assert turn.assistant_text == "ack\nfinal answer"
        # The tool_result payload's own content must never leak in.
        assert "irrelevant" not in turn.assistant_text

    def test_sidechain_entries_excluded(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [
                _user_entry("main prompt"),
                _sidechain_entry("sub1", "subagent output must not surface"),
            ],
        )
        turn = last_turn_context(path)
        # The only non-sidechain content is the user prompt; no assistant
        # turn survives filtering, so nothing "current" was ever produced.
        assert "subagent output must not surface" not in turn.assistant_text
        assert "subagent output must not surface" not in turn.assistant_thinking

    def test_newest_turn_first_and_max_turns_caps(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [
                _user_entry("p1"),
                _assistant_entry("u1", text="first"),
                _user_entry("p2"),
                _assistant_entry("u2", text="second"),
            ],
        )
        turns = list(iter_turns(path, max_turns=1))
        assert len(turns) == 1
        assert turns[0].assistant_text == "second"
        assert turns[0].user_prompt == "p2"

    def test_malformed_lines_are_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "transcript.jsonl"
        p.write_text(
            "not json at all\n"
            + json.dumps(_user_entry("ok prompt"))
            + "\n"
            + json.dumps(_assistant_entry("u1", text="ok text"))
        )
        turn = last_turn_context(str(p))
        assert turn.assistant_text == "ok text"

    def test_no_recoverable_turns_yields_lagging_result(self, tmp_path):
        # Only a user prompt was flushed so far — the assistant turn that
        # would answer it hasn't landed on disk yet (the async-write case).
        path = _write_transcript(tmp_path, [_user_entry("just typed this")])
        turn = last_turn_context(path)
        assert turn.lagging is True
        # ...and the prompt itself is still the freshest signal available,
        # so it is carried rather than discarded (there is simply no
        # previous complete turn here to supply an assistant body).
        assert turn.user_prompt == "just typed this"
        assert turn.assistant_text == ""

    def test_empty_transcript_yields_empty_lagging_result(self, tmp_path):
        # Genuinely nothing recoverable — not even a prompt.
        p = tmp_path / "transcript.jsonl"
        p.write_text("")
        turn = last_turn_context(str(p))
        assert turn.lagging is True
        assert turn.user_prompt == ""
        assert turn.assistant_text == ""


# ---------------------------------------------------------------------------
# build_query — budget resolution.
# ---------------------------------------------------------------------------


class TestBudgetResolution:
    def test_budget_uses_min_across_multiple_models(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="x" * 4000)],
        )
        result_multi = build_query(
            "short trigger",
            transcript_path=path,
            embedding_models=[_QWEN3, _CODESAGE],
        )
        result_codesage_only = build_query(
            "short trigger",
            transcript_path=path,
            embedding_models=[_CODESAGE],
        )
        # The multi-model budget must never exceed the smaller single-model
        # budget (codesage is the large_context/2048-token-class model here;
        # qwen3 is a wider xlarge_context model) — proves MIN, not MAX/first.
        assert result_multi.budget_tokens <= result_codesage_only.budget_tokens

    def test_unknown_model_is_conservative_not_large_context(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="lots of extra context " * 50)],
        )
        result = build_query(
            "short trigger",
            transcript_path=path,
            embedding_models=["totally-unknown-model-xyz"],
        )
        # Conservative fallback: no enrichment attempted for an unknown
        # model, rather than silently assuming the largest preset.
        assert result.enriched is False
        assert result.text == "short trigger"

    def test_empty_model_list_is_also_conservative(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="extra context here")],
        )
        result = build_query("short trigger", transcript_path=path, embedding_models=[])
        assert result.enriched is False

    def test_budget_is_the_embedding_num_ctx_not_the_chunking_preset_max(self):
        # R29: the enrichment budget is the EMBEDDING model's input window.
        # ORIGINALLY: qwen3-embedding's num_ctx is 10 240 but it routed to
        # the xlarge_context preset whose max was 13 500 — sizing on the
        # preset produced a 12 150-token budget, 19% ABOVE the window.
        # D16 (v0.2.92) closed that gap on the chunking side too: preset
        # maxes are now clamped to int(num_ctx * (1 - margin)), so the
        # qwen3 premise line ("preset_max > num_ctx") no longer holds —
        # replaced below by the clamp pin, plus an arctic discriminator
        # (its preset max 3 200 != its window budget 3 686) that keeps the
        # test's original claim ENFORCED: the budget sizes on num_ctx even
        # where the preset max differs from it.
        from claude_mcp_servers.weaviate_mcp.chunking import (
            MODEL_TOKEN_LIMITS,
            chunking_preset_for_model,
        )
        from vco_lib.query_enrichment import _BUDGET_SAFETY_MARGIN_RATIO, _resolve_budget

        num_ctx = MODEL_TOKEN_LIMITS[_QWEN3]
        preset_max = chunking_preset_for_model(_QWEN3)[1]
        assert preset_max <= num_ctx  # D16 clamp: the gap is closed

        budget, _target, conservative = _resolve_budget([_QWEN3])
        assert conservative is False
        assert budget < num_ctx  # strictly under the real window...
        assert budget == int(num_ctx * (1 - _BUDGET_SAFETY_MARGIN_RATIO))

        # The live discriminator: arctic's preset max (medium tier, 3 200)
        # differs from its window budget int(4 096 * 0.9) = 3 686. A
        # _resolve_budget that sized on the preset max would return 3 200.
        _arctic = "snowflake-arctic-embed2:latest"
        arctic_ctx = MODEL_TOKEN_LIMITS[_arctic]
        assert chunking_preset_for_model(_arctic)[1] != int(
            arctic_ctx * (1 - _BUDGET_SAFETY_MARGIN_RATIO)
        )
        arctic_budget, _t, _c = _resolve_budget([_arctic])
        assert arctic_budget == int(arctic_ctx * (1 - _BUDGET_SAFETY_MARGIN_RATIO))

    def test_conservative_fallback_also_applies_the_safety_margin(self):
        # The unknown-model branch used to return the preset max verbatim —
        # the ONE path with no headroom at all.
        from claude_mcp_servers.weaviate_mcp.chunking import CHUNKING_PRESETS
        from vco_lib.query_enrichment import _BUDGET_SAFETY_MARGIN_RATIO, _resolve_budget

        small_max = CHUNKING_PRESETS["small_context"][1]
        budget, _target, conservative = _resolve_budget(["totally-unknown-model-xyz"])
        assert conservative is True
        assert budget < small_max
        assert budget == int(small_max * (1 - _BUDGET_SAFETY_MARGIN_RATIO))


# ---------------------------------------------------------------------------
# build_query — enrichment vs untouched-when-long.
# ---------------------------------------------------------------------------


class TestEnrichmentDecision:
    def test_short_trigger_gets_enriched_from_transcript(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [
                _user_entry("investigate the flaky retry test"),
                _assistant_entry(
                    "u1",
                    text="the retry test fails when the mock clock drifts",
                    thinking="checking the clock fixture next",
                    tool_use="Read",
                ),
            ],
        )
        result = build_query(
            "flaky test",  # well under the 24-token default threshold
            transcript_path=path,
            embedding_models=[_QWEN3],
        )
        assert isinstance(result, EnrichedQuery)
        assert result.enriched is True
        assert result.text.startswith("flaky test")
        assert "the retry test fails when the mock clock drifts" in result.text
        assert "checking the clock fixture next" in result.text
        assert "investigate the flaky retry test" in result.text
        assert set(result.sources) <= {"user_prompt", "assistant_text", "assistant_thinking"}
        assert result.added_tokens > 0
        assert result.digest  # non-empty sha1 hex digest
        assert len(result.digest) == 40

    def test_long_trigger_is_never_enriched(self, tmp_path):
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="plenty of extra material " * 30)],
        )
        long_trigger = "explain the retry backoff mechanism in exhaustive detail " * 5
        result = build_query(
            long_trigger,
            transcript_path=path,
            embedding_models=[_QWEN3],
            short_threshold_tokens=DEFAULT_SHORT_THRESHOLD_TOKENS,
        )
        assert result.enriched is False
        assert result.text == long_trigger
        assert result.sources == ()
        assert result.added_tokens == 0

    def test_no_transcript_material_leaves_trigger_unchanged(self, tmp_path):
        # A transcript whose only entry is a tool_result echo: no genuine
        # user prompt, no assistant turn — nothing to enrich WITH.
        path = _write_transcript(tmp_path, [_tool_result_entry()])
        result = build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        assert result.enriched is False
        assert result.text == "short trigger"

    def test_lagging_transcript_falls_back_to_previous_turn(self, tmp_path):
        # The newest user prompt has no assistant turn yet (async-write lag).
        # Two things must hold, and pre-v0.2.92 NEITHER did: the result must
        # be MARKED lagging, and the freshly-read prompt must be the one used
        # — the previous turn supplies only the assistant BODY fallback, never
        # the prompt, or every first-tool-call-of-a-turn enrichment describes
        # the task the user has already left.
        path = _write_transcript(
            tmp_path,
            [
                _user_entry("previous prompt"),
                _assistant_entry("u1", text="previous complete answer"),
                _user_entry("brand new prompt not yet answered"),
            ],
        )
        turn = last_turn_context(path)
        assert turn.lagging is True
        assert turn.user_prompt == "brand new prompt not yet answered"
        assert turn.user_prompt != "previous prompt"
        assert turn.assistant_text == "previous complete answer"

        result = build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        assert result.enriched is True
        assert "previous complete answer" in result.text
        # The pending prompt IS used; the stale one is NOT.
        assert "brand new prompt not yet answered" in result.text
        assert "previous prompt" not in result.text


# ---------------------------------------------------------------------------
# Env knobs — each independently observable per CLAUDE.md's R24-style rule.
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_env_off_disables_unconditionally(self, tmp_path, monkeypatch):
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="would have enriched with this")],
        )
        monkeypatch.setenv(ENV_ENABLE, "off")
        result = build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        assert result.enriched is False
        assert result.text == "short trigger"

        disabled_via_kwarg = build_query(
            "short trigger", transcript_path=path, embedding_models=[_QWEN3], enabled=False
        )
        assert result == disabled_via_kwarg

    def test_env_short_threshold_overrides_kwarg(self, tmp_path, monkeypatch):
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="extra material to add")],
        )
        # A trigger that is short enough to enrich under the built-in
        # default, but the env forces the threshold down to 0 tokens so
        # nothing qualifies as "short" any more.
        monkeypatch.setenv(ENV_SHORT_THRESHOLD, "0")
        result = build_query(
            "tiny",
            transcript_path=path,
            embedding_models=[_QWEN3],
            short_threshold_tokens=100,  # kwarg says "very generous"
        )
        assert result.enriched is False  # env (0) won, not the kwarg (100)

    def test_env_share_changes_added_token_ceiling(self, tmp_path, monkeypatch):
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="filler word " * 2000)],
        )
        monkeypatch.setenv(ENV_SHARE, "0.01")
        tiny_share = build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        monkeypatch.setenv(ENV_SHARE, "0.9")
        large_share = build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        assert tiny_share.added_tokens < large_share.added_tokens

    def test_env_share_is_clamped_to_a_sane_range(self, tmp_path, monkeypatch):
        # ``share`` is a FRACTION of the target chunk, so >1 is nonsense. It
        # is observable on a model whose target sits well below its budget
        # (codesage: target 1100, budget 1843) — there the unclamped value
        # abandons the share ceiling entirely and runs to the budget.
        from vco_lib.query_enrichment import SHARE_MAX, SHARE_MIN

        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="filler word " * 2000)],
        )
        monkeypatch.setenv(ENV_SHARE, str(SHARE_MAX))
        at_ceiling = build_query(
            "short trigger", transcript_path=path, embedding_models=[_CODESAGE]
        )
        monkeypatch.setenv(ENV_SHARE, "99")  # nonsense: 99x the target chunk
        above_ceiling = build_query(
            "short trigger", transcript_path=path, embedding_models=[_CODESAGE]
        )
        assert at_ceiling.enriched is True
        # Clamped, so an absurd value buys nothing beyond the ceiling.
        assert above_ceiling.added_tokens == at_ceiling.added_tokens

        # The floor: ``max(0, ...)`` already absorbs a negative ceiling, so
        # SHARE_MIN is defensive rather than behavioural — pin the outcome so
        # a future refactor that drops the ``max(0, ...)`` is still caught.
        monkeypatch.setenv(ENV_SHARE, str(SHARE_MIN))
        at_floor = build_query(
            "short trigger", transcript_path=path, embedding_models=[_CODESAGE]
        )
        monkeypatch.setenv(ENV_SHARE, "-5")
        below_floor = build_query(
            "short trigger", transcript_path=path, embedding_models=[_CODESAGE]
        )
        assert at_floor.enriched is False
        assert below_floor == at_floor

    def test_malformed_env_values_fall_back_to_defaults(self, tmp_path, monkeypatch):
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="some material")],
        )
        monkeypatch.setenv(ENV_SHORT_THRESHOLD, "not-a-number")
        monkeypatch.setenv(ENV_SHARE, "also-not-a-number")
        # Must not raise despite garbage env values.
        result = build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        assert isinstance(result, EnrichedQuery)


# ---------------------------------------------------------------------------
# Privacy — the transcript text must never appear anywhere but result.text.
# ---------------------------------------------------------------------------


class TestPrivacyDiscipline:
    def test_digest_never_contains_raw_added_text(self, tmp_path):
        secret_marker = "UNIQUE_SECRET_TRANSCRIPT_MARKER_98765"
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text=secret_marker)],
        )
        result = build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        assert result.enriched is True
        assert secret_marker not in result.digest
        # digest is a hex sha1 — fixed shape, never raw text length-dependent
        # on the marker's own length.
        assert all(c in "0123456789abcdef" for c in result.digest)

    def test_never_raises_on_unreadable_transcript_path(self):
        # A directory, not a file — must degrade gracefully, never raise,
        # and never let an OS error message leak into an exception surface.
        result = build_query("short trigger", transcript_path="/", embedding_models=[_QWEN3])
        assert isinstance(result, EnrichedQuery)
        assert result.enriched is False

    def test_transcript_text_never_appears_in_a_raised_exception(self, tmp_path, monkeypatch):
        # The module docstring's claim, made testable: if anything inside the
        # enrichment path DOES raise while transcript text is in scope, the
        # text must not ride out on the exception. Provoked by breaking the
        # collaborator that runs with every candidate field in hand.
        from claude_mcp_servers.weaviate_mcp.chunking import TokenCounter

        marker = "UNIQUE_FIXTURE_MARKER_FOR_EXC_CHECK_31415"
        path = _write_transcript(
            tmp_path,
            [
                _user_entry(f"{marker} in the user prompt"),
                _assistant_entry(
                    "u1",
                    text=f"{marker} in the assistant text",
                    thinking=f"{marker} in the assistant thinking",
                ),
            ],
        )

        # Fail on the SECOND count onward: the first counts the trigger, so
        # failing there would abort before any transcript byte is read. From
        # the second on, every candidate field is live in the frame.
        calls = {"n": 0}
        real = TokenCounter.count_tokens

        def _boom_after_first(text):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("tokenizer unavailable")
            return real(text)

        monkeypatch.setattr(TokenCounter, "count_tokens", staticmethod(_boom_after_first))
        try:
            build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        except BaseException as exc:  # noqa: BLE001 -- the surface under test
            rendered = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ) + repr(exc)
            assert marker not in rendered, (
                "transcript text leaked into a raised exception's message/traceback"
            )

    @pytest.mark.parametrize(
        "rel_path",
        ["vco_lib/query_enrichment.py", "vco_lib/transcript_context.py"],
    )
    def test_modules_never_log_or_print(self, rel_path):
        # The static half of the same claim: neither module has a logging or
        # print surface that COULD carry transcript text, so "never logged"
        # is a structural property rather than a promise.
        body = (_repo_root() / rel_path).read_text(encoding="utf-8")
        code_lines = [
            line
            for line in body.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for line in code_lines:
            assert "print(" not in line, f"{rel_path}: print() surface: {line}"
            assert "logger." not in line, f"{rel_path}: logger surface: {line}"
            assert "sys.stderr" not in line, f"{rel_path}: stderr surface: {line}"

    def test_observation_row_carries_counts_and_digest_but_never_text(
        self, tmp_path, monkeypatch
    ):
        # m11 silence detector: digest/sources/added_tokens now have a
        # consumer, so "did enrichment ever fire" is answerable from disk.
        from vco_lib.paths import vct_metrics_dir
        from vco_lib.query_enrichment import OBSERVATION_STREAM

        state = tmp_path / "vct-state"
        monkeypatch.setenv("VCT_STATE_DIR", str(state))
        marker = "UNIQUE_OBSERVATION_MARKER_27182"
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text=marker)],
        )
        result = build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])
        assert result.enriched is True

        stream = vct_metrics_dir() / OBSERVATION_STREAM
        rows = [json.loads(line) for line in stream.read_text().splitlines() if line.strip()]
        assert rows, "enrichment fired but emitted no observation row (silent again)"
        row = rows[-1]
        assert row["enriched"] is True
        assert row["digest"] == result.digest
        assert row["added_tokens"] == result.added_tokens
        assert row["sources"] == list(result.sources)
        # Privacy: counts + digest only — never the text, trigger, or path.
        raw = stream.read_text()
        assert marker not in raw
        assert "short trigger" not in raw
        assert str(path) not in raw

    def test_declined_enrichment_is_also_observable(self, tmp_path, monkeypatch):
        # Silence must be distinguishable from "never called": a decline
        # emits a row too, marked enriched=false.
        from vco_lib.paths import vct_metrics_dir
        from vco_lib.query_enrichment import OBSERVATION_STREAM

        state = tmp_path / "vct-state"
        monkeypatch.setenv("VCT_STATE_DIR", str(state))
        monkeypatch.setenv(ENV_ENABLE, "off")
        path = _write_transcript(
            tmp_path,
            [_user_entry("p"), _assistant_entry("u1", text="would have enriched")],
        )
        build_query("short trigger", transcript_path=path, embedding_models=[_QWEN3])

        stream = vct_metrics_dir() / OBSERVATION_STREAM
        rows = [json.loads(line) for line in stream.read_text().splitlines() if line.strip()]
        assert rows and rows[-1]["enriched"] is False


# ---------------------------------------------------------------------------
# SSOT parity — the same budget-resolution primitives this module calls are
# the ones search_knowledge.py / query_code_graph.py / code_truncation.py
# consume, so a change to chunking.py's presets propagates everywhere at
# once (this is the assertion that the "one shared component" claim holds).
# ---------------------------------------------------------------------------


class TestSSotParityAcrossConsumers:
    def test_query_enrichment_budget_and_query_chunking_model_max_agree_in_kind(self):
        from claude_mcp_servers.rl_client.query_chunking import model_max_tokens
        from claude_mcp_servers.weaviate_mcp.chunking import (
            CHUNK_TOKEN_POLICY_CEILING,
            MODEL_TOKEN_LIMITS,
            _BUDGET_SAFETY_MARGIN_RATIO,
        )
        from vco_lib.query_enrichment import _resolve_budget

        budget_tokens, _target, conservative = _resolve_budget([_QWEN3])
        assert conservative is False
        chunk_max = model_max_tokens(_QWEN3)
        # R29 vs R39 (v0.2.92): the ENRICHMENT budget sizes on the embedding
        # model's WINDOW (R29 — the enriched query is embedded through the
        # same num_ctx), while the CHUNK max is the R39 retrieval-quality
        # policy, min(policy, window budget). For qwen3 those legitimately
        # differ (9 216 vs 8 192): a query is not a chunk, and R39's
        # fragment-match rationale governs indexed chunks, not the query
        # side. What must hold IN KIND: both derive from chunking.py's
        # MODEL_TOKEN_LIMITS, the enrichment budget equals the window
        # budget and never exceeds the window itself, and the chunk max
        # never exceeds either bound (the policy can only TIGHTEN).
        window_budget = int(MODEL_TOKEN_LIMITS[_QWEN3] * (1 - _BUDGET_SAFETY_MARGIN_RATIO))
        assert budget_tokens == window_budget == 9_216
        assert budget_tokens <= MODEL_TOKEN_LIMITS[_QWEN3] == 10_240
        assert chunk_max == min(window_budget, CHUNK_TOKEN_POLICY_CEILING) == 8_192
        assert chunk_max <= budget_tokens


# --------------------------------------------------------------------------
# Call-site textual-adjacency pins: every real codegraph_query_block /
# Invoke-VcoCodegraphQueryBlock invocation must literally forward
# prompt_id/transcript. This is the "correct-but-undelivered" defect class
# this release exists to remove: the shared function can be fully correct
# (see tests/test_query_cache_v0277.py's unit-level coverage of
# codegraph_query_block itself) while a hook's call site silently drops the
# new args, leaving the capability unreachable in practice. Mirrors the
# existing --hook-format pin discipline in
# tests/test_pre_edit_hook_dedup_regression.py
# (_producer_invocation_lines + window-based flag-presence assertion).
# --------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _non_comment_lines_containing(body: str, needle: str) -> list[tuple[int, str]]:
    """Return (0-based line index, line text) for lines containing `needle`
    whose first non-whitespace character is not '#' (i.e. not a comment).
    """
    out: list[tuple[int, str]] = []
    for i, line in enumerate(body.splitlines()):
        if needle not in line:
            continue
        if line.lstrip().startswith("#"):
            continue
        out.append((i, line))
    return out


# (hook file, expected minimum count of real codegraph_query_block calls)
_SH_CODEGRAPH_CALL_SITES = [
    "templates/hooks/pre-tool-use.sh",
    "templates/hooks/pre-bash-context-inject.sh",
    "templates/hooks/pre-edit-context-inject.sh",
]

_PS1_CODEGRAPH_CALL_SITES = [
    "templates/hooks/pre-tool-use.ps1",
    "templates/hooks/pre-bash-context-inject.ps1",
    "templates/hooks/pre-edit-context-inject.ps1",
]

# The KG-side wrappers are the OTHER half of the same capability and were
# unpinned until v0.2.92 (m7): a refactor dropping $TRANSCRIPT_PATH from any
# of them would leave KG retrieval unenriched with every test still green —
# the same "correct-but-undelivered" shape the codegraph pins above exist to
# catch. Same file set, same flags, one more function name each.
_SH_KG_CALL_SITES = [
    "templates/hooks/pre-tool-use.sh",
    "templates/hooks/pre-bash-context-inject.sh",
    "templates/hooks/pre-edit-context-inject.sh",
]

_PS1_KG_CALL_SITES = [
    "templates/hooks/pre-tool-use.ps1",
    "templates/hooks/pre-bash-context-inject.ps1",
    "templates/hooks/pre-edit-context-inject.ps1",
]

#: Shell wrappers that carry the KG leg. ``vco_dual_search_cached`` runs the
#: KG + code-graph pair in one interpreter (pre-edit only), so it must forward
#: the flags too or the merged fast path silently loses what the legacy
#: two-process fallback keeps.
_SH_KG_FUNCS = ("vco_kg_search_cached", "vco_dual_search_cached")
_PS1_KG_FUNCS = ("Invoke-VcoKgSearchCached", "Invoke-VcoDualSearchCached")


def _logical_lines(body: str, continuation: str) -> list[tuple[int, str]]:
    """Fold ``continuation``-terminated line continuations into one entry.

    Returns ``(0-based index of the FIRST physical line, joined text)``. The
    dual-search invocations span several physical lines (``\\`` in bash,
    backtick in PowerShell) with the WP-E flags on the last one, so a
    per-physical-line scan would report a false miss.
    """
    out: list[tuple[int, str]] = []
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        start = i
        joined = lines[i]
        while joined.rstrip().endswith(continuation) and i + 1 < len(lines):
            i += 1
            head = joined.rstrip()[: -len(continuation)].rstrip()
            # Collapse to ONE space: the continuation marker is preceded by a
            # space in both shells, so a naive join yields "fn  \"arg\"" and a
            # `fn "` needle silently misses the very calls this fold exists to
            # reach.
            joined = head + " " + lines[i].strip()
        out.append((start, joined))
        i += 1
    return out


class TestHookCallSitesForwardTranscript:
    """WP-E coordinator condition 2: pin the flag at every real call site
    so a future edit that silently drops $PROMPT_ID / $TRANSCRIPT_PATH (or
    the .ps1 -PromptId / -TranscriptPath equivalents) fails a test instead
    of shipping an unreachable capability.
    """

    @pytest.mark.parametrize("rel_path", _SH_CODEGRAPH_CALL_SITES)
    def test_sh_hook_forwards_prompt_id_and_transcript_to_codegraph_query_block(
        self, rel_path: str
    ) -> None:
        hook_path = _repo_root() / rel_path
        body = hook_path.read_text(encoding="utf-8")
        invocations = [
            (i, line)
            for i, line in _non_comment_lines_containing(body, "codegraph_query_block")
            # Exclude the `command -v codegraph_query_block` availability
            # probes and any other non-invocation mention (e.g. definition
            # comments referencing the function name) — a real invocation
            # opens with `codegraph_query_block "` (quoted first arg).
            if 'codegraph_query_block "' in line
        ]
        assert invocations, (
            f"{rel_path}: no real codegraph_query_block invocation found "
            f"(only non-invocation mentions such as `command -v` probes)."
        )
        for i, line in invocations:
            assert "$PROMPT_ID" in line, (
                f"{rel_path}:{i + 1}: codegraph_query_block call is missing "
                f"$PROMPT_ID — this silently drops cache-key scoping across "
                f"turns (R31/coordinator condition 4):\n{line}"
            )
            assert "$TRANSCRIPT_PATH" in line, (
                f"{rel_path}:{i + 1}: codegraph_query_block call is missing "
                f"$TRANSCRIPT_PATH — this silently drops code-graph query "
                f"enrichment (R29/R30), reproducing the exact "
                f"'correct-but-undelivered' defect class this release "
                f"targets:\n{line}"
            )

    @pytest.mark.parametrize("rel_path", _PS1_CODEGRAPH_CALL_SITES)
    def test_ps1_hook_forwards_prompt_id_and_transcript_to_invoke_codegraph_query_block(
        self, rel_path: str
    ) -> None:
        hook_path = _repo_root() / rel_path
        body = hook_path.read_text(encoding="utf-8")
        invocations = [
            (i, line)
            for i, line in _non_comment_lines_containing(
                body, "Invoke-VcoCodegraphQueryBlock"
            )
            # Exclude the `Get-Command Invoke-VcoCodegraphQueryBlock`
            # availability probes — a real invocation assigns the result
            # (`= Invoke-VcoCodegraphQueryBlock -Query ...`).
            if "= Invoke-VcoCodegraphQueryBlock " in line
        ]
        assert invocations, (
            f"{rel_path}: no real Invoke-VcoCodegraphQueryBlock invocation "
            f"found (only non-invocation mentions such as Get-Command probes)."
        )
        for i, line in invocations:
            assert "-PromptId $PromptId" in line, (
                f"{rel_path}:{i + 1}: Invoke-VcoCodegraphQueryBlock call is "
                f"missing -PromptId $PromptId — this silently drops "
                f"cache-key scoping across turns (R31/coordinator "
                f"condition 4):\n{line}"
            )
            assert "-TranscriptPath $TranscriptPath" in line, (
                f"{rel_path}:{i + 1}: Invoke-VcoCodegraphQueryBlock call is "
                f"missing -TranscriptPath $TranscriptPath — this silently "
                f"drops code-graph query enrichment (R29/R30), reproducing "
                f"the exact 'correct-but-undelivered' defect class this "
                f"release targets:\n{line}"
            )


class TestHookCallSitesForwardTranscriptToKgSearch:
    """m7 (v0.2.92): the same pin, for the KG half of every hook.

    ``TestHookCallSitesForwardTranscript`` covered only the six code-graph
    invocations. The KG invocations forwarded correctly but nothing held them
    there, so a refactor could drop ``$PROMPT_ID`` / ``$TRANSCRIPT_PATH`` and
    ship an unreachable capability with a green suite.
    """

    @pytest.mark.parametrize("rel_path", _SH_KG_CALL_SITES)
    def test_sh_hook_forwards_prompt_id_and_transcript_to_kg_search(
        self, rel_path: str
    ) -> None:
        hook_path = _repo_root() / rel_path
        body = hook_path.read_text(encoding="utf-8")
        invocations: list[tuple[int, str]] = []
        for i, line in _logical_lines(body, "\\"):
            if line.lstrip().startswith("#"):
                continue
            # A real invocation names the function followed by its first
            # argument, never `command -v <fn>` / a bare mention in prose.
            for fn in _SH_KG_FUNCS:
                if f"{fn} \"" in line and "command -v" not in line:
                    invocations.append((i, line))
                    break
        assert invocations, (
            f"{rel_path}: no real vco_kg_search_cached / vco_dual_search_cached "
            f"invocation found (only non-invocation mentions such as "
            f"`command -v` probes)."
        )
        for i, line in invocations:
            assert "$PROMPT_ID" in line, (
                f"{rel_path}:{i + 1}: KG search call is missing $PROMPT_ID — "
                f"this silently drops cache-key scoping across turns, so a "
                f"later turn replays an earlier turn's enriched result "
                f"(R31/coordinator condition 4):\n{line}"
            )
            assert "$TRANSCRIPT_PATH" in line, (
                f"{rel_path}:{i + 1}: KG search call is missing "
                f"$TRANSCRIPT_PATH — this silently drops KG query enrichment "
                f"(R29/R30), reproducing the exact 'correct-but-undelivered' "
                f"defect class this release targets:\n{line}"
            )

    @pytest.mark.parametrize("rel_path", _PS1_KG_CALL_SITES)
    def test_ps1_hook_forwards_prompt_id_and_transcript_to_kg_search(
        self, rel_path: str
    ) -> None:
        hook_path = _repo_root() / rel_path
        body = hook_path.read_text(encoding="utf-8")
        invocations: list[tuple[int, str]] = []
        for i, line in _logical_lines(body, "`"):
            if line.lstrip().startswith("#"):
                continue
            for fn in _PS1_KG_FUNCS:
                if f"= {fn} " in line:
                    invocations.append((i, line))
                    break
        assert invocations, (
            f"{rel_path}: no real Invoke-VcoKgSearchCached / "
            f"Invoke-VcoDualSearchCached invocation found (only "
            f"non-invocation mentions such as Get-Command probes)."
        )
        for i, line in invocations:
            assert "-PromptId $PromptId" in line, (
                f"{rel_path}:{i + 1}: KG search call is missing "
                f"-PromptId $PromptId — this silently drops cache-key scoping "
                f"across turns (R31/coordinator condition 4):\n{line}"
            )
            assert "-TranscriptPath $TranscriptPath" in line, (
                f"{rel_path}:{i + 1}: KG search call is missing "
                f"-TranscriptPath $TranscriptPath — this silently drops KG "
                f"query enrichment (R29/R30), reproducing the exact "
                f"'correct-but-undelivered' defect class this release "
                f"targets:\n{line}"
            )

    def test_pin_sees_the_multi_line_dual_search_invocation(self) -> None:
        """The pin must not pass by simply finding nothing to check.

        ``vco_dual_search_cached`` / ``Invoke-VcoDualSearchCached`` are the
        merged-fast-path calls and are the only ones spanning several physical
        lines; a naive per-line scan would silently skip them and the
        assertions above would then be vacuous for pre-edit.
        """
        sh = (_repo_root() / "templates/hooks/pre-edit-context-inject.sh").read_text(
            encoding="utf-8"
        )
        sh_hits = [
            line
            for _i, line in _logical_lines(sh, "\\")
            if 'vco_dual_search_cached "' in line and not line.lstrip().startswith("#")
        ]
        assert sh_hits, "pre-edit-context-inject.sh: dual-search invocation not located"
        assert all("$TRANSCRIPT_PATH" in line for line in sh_hits)

        ps1 = (_repo_root() / "templates/hooks/pre-edit-context-inject.ps1").read_text(
            encoding="utf-8"
        )
        ps1_hits = [
            line
            for _i, line in _logical_lines(ps1, "`")
            if "= Invoke-VcoDualSearchCached " in line
            and not line.lstrip().startswith("#")
        ]
        assert ps1_hits, "pre-edit-context-inject.ps1: dual-search invocation not located"
        assert all("-TranscriptPath $TranscriptPath" in line for line in ps1_hits)
