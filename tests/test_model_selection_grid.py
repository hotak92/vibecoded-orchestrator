# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``{{MODEL_SELECTION_GRID}}`` renderer — vco_lib/model_selection.py.

Two things are pinned here:

* the FILTER — which rows render for which set of reachable providers
  (Claude always; z.ai adds the GLM rows; the qwen vendor adds its own rows
  AND GLM-5.3, which both vendors serve), and the soft-fail contract (a
  resolution error or a missing gateway package leaves the Claude rows
  standing, one stderr line, no crash);
* the SCORES — the data file must carry exactly the source grid's values
  (the owner-maintained "Model selection — which model for which task"
  table, 2026-10-03 revision), so a regeneration or fat-fingered edit of
  the .toml cannot silently re-rank a model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from vco_lib import materialize, model_selection

#: The source grid (medium-effort re-score, 2026-10-03), transcribed
#: verbatim: row id -> the 11 column scores, in the source's row ORDER.
#: Columns: Code, Debug, Review, Plan, Bulk, Tools, Instr, LongCtx, Docs,
#: Vision, Cost.
EXPECTED_SCORES = {
    "opus": ["10", "10", "9", "9", "5", "10", "8*", "10", "9", "10", "4"],
    "fable": ["9", "10", "10", "10", "3", "9", "6", "10", "10", "10", "1"],
    "glm-5.3": ["7", "6", "7", "6", "6", "6", "7*", "7", "7", "—", "8"],
    "deepseek-v4.1-flash": ["7", "6", "6", "6", "9", "6", "7*", "8", "6", "7", "10"],
    "sonnet": ["6", "6", "5*", "5", "6", "5", "6*", "7", "7", "8*", "6"],
    "qwen3.8-max": ["6", "6", "5", "6*", "4", "5", "8", "8", "9", "8", "3"],
    "glm-5.3-flash": ["5", "5", "5", "5", "8", "5", "6*", "7", "6*", "8", "8"],
    "qwen3.8-flash": ["5*", "4", "4*", "4*", "7", "4", "8*", "7", "5*", "7", "9"],
    "haiku": ["2", "2", "2*", "2", "5", "3", "5", "5", "2*", "4", "7"],
}

#: Rows carrying the `†` marker (only xhigh/max benchmark results).
EXPECTED_DAGGERS = {"glm-5.3", "qwen3.8-max", "glm-5.3-flash", "qwen3.8-flash"}

EXPECTED_COLUMNS = ["Code", "Debug", "Review", "Plan", "Bulk", "Tools",
                    "Instr", "LongCtx", "Docs", "Vision", "Cost"]

#: The "What changes the pick:" bullets that belong to ONE model, transcribed
#: verbatim (row id -> note text), in the source's order.
EXPECTED_NOTES = {
    "opus": "**Opus 5.5** leads at medium on almost every axis and costs less "
            "per task than Fable 5.1 (≈$1.3 vs ≈$3.0); keep Fable for final "
            "reviews, plans and the hardest debugging.",
    "sonnet": "**Sonnet 5.5** is the most effort-sensitive model: near Opus at "
              "max, but at medium it trails GLM-5.3 and DeepSeek V4.1 Flash on "
              "coding. Not a default choice.",
    "deepseek-v4.1-flash": "**DeepSeek V4.1 Flash** — fastest (≈209 tok/s, ≈1 s "
                           "to first token) and cheapest per task: default for "
                           "research, surveys, bulk and bounded implementation. "
                           "Users report fragile tool calls / loops on long runs "
                           "— keep tasks bounded.",
    "glm-5.3": "**GLM-5.3** — the non-Claude model users route review/audit to; "
               "text-only.",
    "glm-5.3-flash": "**GLM-5.3-Flash** suits bulk / first-pass work behind a "
                     "verifier — tests, a diff review or a stronger second pass; "
                     "retries help it, more effort does not (longer runs helped "
                     "it only 46% of the time, below chance). Keep it off "
                     "shared-component extractions, where one un-migrated "
                     "call-site leaves the suite green while defeating the "
                     "change, and never give it unsupervised writes to a green "
                     "test suite (it broke a passing baseline test in 6.9% of "
                     "rollouts).",
    "qwen3.8-max": "**Qwen3.8-Max** — best prose and literal instruction-"
                   "following, but it defaults to xhigh effort (≈$5.4 per task "
                   "there) and users report it leaves work half-done and skips "
                   "tests unless asked: state \"add tests and run them\".",
}

#: The model-INDEPENDENT caveats — rendered for every grid, whatever the
#: reachable providers.
EXPECTED_GENERAL_NOTES = [
    "No benchmark measures code review for these versions; that column leans "
    "on user comparison reports.",
    "Re-check a row when that model's minor version changes — scores are tied "
    "to the exact versions above.",
]

CLAUDE_ROWS = ("Opus 5.5", "Fable 5.1", "Sonnet 5.5", "Haiku 4.5")


def _rows_rendered(markdown: str) -> set[str]:
    return {line.split(" (`", 1)[0].lstrip("| ")
            for line in markdown.splitlines()
            if line.startswith("| ") and " (`" in line and "reach via" not in line}


class TestProviderFilter:
    def test_claude_only_when_no_vendor_key(self):
        md = model_selection.render_grid(providers=frozenset({"anthropic"}))
        assert _rows_rendered(md) == set(CLAUDE_ROWS)
        # The Claude notes render; every VENDOR note (and row) stays out.
        # (The Sonnet note NAMES GLM/DeepSeek in prose, so the row/note
        # fragments below are the discriminators, not bare model names.)
        assert "leads at medium on almost every axis" in md
        assert "6.9% of rollouts" not in md          # GLM-5.3-Flash note
        assert "fragile tool calls" not in md        # DeepSeek note
        assert "best prose" not in md                # Qwen3.8-Max note
        assert "GLM-5.3-Flash" not in md and "Qwen3.8" not in md

    def test_zai_key_adds_the_glm_rows_only(self):
        md = model_selection.render_grid(providers=frozenset({"anthropic", "zai"}))
        assert _rows_rendered(md) == set(CLAUDE_ROWS) | {"GLM-5.3 †", "GLM-5.3-Flash †"}
        assert "the non-Claude model users route review/audit to" in md
        assert "6.9% of rollouts" in md
        assert "DeepSeek V4.1 Flash (`" not in md and "Qwen3.8" not in md
        assert "fragile tool calls" not in md

    def test_qwen_key_adds_qwen_vendor_rows_including_glm_5_3(self):
        """The qwen vendor serves GLM-5.3 too, but NOT GLM-5.3-Flash
        (z.ai-only) — the row that proves the providers are per-row, not
        per-vendor-block."""
        md = model_selection.render_grid(providers=frozenset({"anthropic", "qwen"}))
        assert _rows_rendered(md) == set(CLAUDE_ROWS) | {
            "GLM-5.3 †", "DeepSeek V4.1 Flash", "Qwen3.8-Max †", "Qwen3.8-Flash †",
        }
        assert "GLM-5.3-Flash" not in md
        assert "best prose and literal instruction-following" in md

    def test_both_keys_render_every_row(self):
        md = model_selection.render_grid(
            providers=frozenset({"anthropic", "zai", "qwen"}))
        all_models = {f"{r.model} †" if r.dagger else r.model
                      for r in model_selection.load_grid().rows}
        assert _rows_rendered(md) == all_models


class TestSoftFail:
    def test_key_probe_exception_leaves_claude_rows_and_never_crashes(self, capsys):
        def boom(_vendor_id: str) -> bool:
            raise RuntimeError("hub exploded")

        providers = model_selection.reachable_providers(key_probe=boom)
        assert providers == model_selection.ALWAYS_PROVIDERS
        err = capsys.readouterr().err
        assert "RuntimeError" in err and err.count("\n") == 2  # one line per vendor
        # The warning names only the exception TYPE: an exception message could
        # one day carry a resolved key value, which must never reach a message.
        assert "hub exploded" not in err
        md = model_selection.render_grid(providers=providers)
        assert _rows_rendered(md) == set(CLAUDE_ROWS)

    def test_gateway_not_importable_renders_claude_only(self, monkeypatch, capsys):
        # An import of `model_router.secrets` answered by None in sys.modules
        # raises ImportError — the "gateway not installed/active" case.
        monkeypatch.setitem(sys.modules, "model_router", None)
        monkeypatch.setitem(sys.modules, "model_router.secrets", None)
        providers = model_selection.reachable_providers()
        assert providers == model_selection.ALWAYS_PROVIDERS
        assert "model gateway not importable" in capsys.readouterr().err


class TestGridData:
    def test_scores_match_the_source_grid(self):
        grid = model_selection.load_grid()
        assert list(grid.columns) == EXPECTED_COLUMNS
        # dict-from-rows compares ids AND scores; the list() around it pins
        # the source's row ORDER too.
        assert {row.id: list(row.scores) for row in grid.rows} == EXPECTED_SCORES
        assert [row.id for row in grid.rows] == list(EXPECTED_SCORES)

    def test_dagger_markers_match_the_source_grid(self):
        grid = model_selection.load_grid()
        assert {row.id for row in grid.rows if row.dagger} == EXPECTED_DAGGERS
        md = model_selection.render_grid(providers=frozenset({"anthropic", "zai"}))
        assert "| GLM-5.3 † (" in md and "**`†`**" in md

    def test_notes_match_the_source_grid(self):
        """The per-model notes AND the model-independent general notes are the
        source grid's "What changes the pick" bullets, verbatim — so a
        regeneration cannot silently drop a guardrail or a caveat."""
        grid = model_selection.load_grid()
        assert [(list(n.models), n.text) for n in grid.notes] == [
            ([rid], text) for rid, text in EXPECTED_NOTES.items()
        ]
        assert list(grid.general_notes) == EXPECTED_GENERAL_NOTES

    def test_general_notes_render_for_every_grid(self):
        """The general notes are not tied to a row: every render carries them."""
        for providers in (
            frozenset({"anthropic"}),
            frozenset({"anthropic", "zai"}),
            frozenset({"anthropic", "qwen"}),
            frozenset({"anthropic", "zai", "qwen"}),
        ):
            md = model_selection.render_grid(providers=providers)
            assert "What changes the pick:" in md
            for note in EXPECTED_GENERAL_NOTES:
                assert note in md, (providers, note)

    def test_reach_via_names_shipped_agents_or_model_values(self):
        """Every gateway reach-via names agents that actually SHIP in
        templates/agents/module-gateway/ (pinned by listing the directory),
        and every Claude row names a `model:` value, not an agent."""
        import tomllib

        data = tomllib.loads(model_selection.GRID_PATH.read_text(encoding="utf-8"))
        shipped = {p.stem for p in
                   (Path(__file__).resolve().parent.parent
                    / "templates" / "agents" / "module-gateway").glob("*.md")}
        for row in data["row"]:
            if "anthropic" in row["providers"]:
                assert row["reach_via"].startswith("model: "), row
                continue
            for agent in row["reach_via"].split(" / "):
                assert agent.strip() in shipped, (
                    f"reach-via names unshipped agent {agent!r}"
                )


class TestRegistry:
    def test_registry_exposes_the_key(self, tmp_path, monkeypatch):
        assert "MODEL_SELECTION_GRID" in materialize.REGISTRY
        key = materialize.REGISTRY["MODEL_SELECTION_GRID"]
        assert key.resolver is not None
        sentinel = "| sentinel grid |"
        monkeypatch.setattr(model_selection, "render_grid", lambda **_kw: sentinel)
        ctx = materialize.MaterializeContext(Path(tmp_path))
        assert key.resolver(ctx) == sentinel

    def test_rendered_grid_is_plain_markdown_safe_for_templates(self):
        """No placeholder token may hide inside the value: the materializer
        substitutes it into markdown bodies verbatim."""
        md = model_selection.render_grid(providers=frozenset({"anthropic"}))
        assert not materialize.PLACEHOLDER_RE.findall(md)
        assert md.startswith("Delegate to subagents")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
