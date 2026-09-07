# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D16 (v0.2.92): the chunker's token budget derives from the EMBEDDING
model's input window (its ``num_ctx`` in ``MODEL_TOKEN_LIMITS``), counted
in ONE deterministic conservative unit — never a chat-model tokenizer,
never a table entry above the window.

Measured magnitude before the fix (2026-09-04, Ollama 0.20.2, corpus =
this repo's KG nodes / docs / ``chunking.py`` itself):

  * The DEPLOYED counter was the chars approximation — ``langchain_ollama``
    is an optional dependency absent from every venv and requirements file
    this repo ships, so ``TokenCounter`` always took the ``len // 4`` path.
    It sits within −1.5 %…+7 % of ``qwen3-embedding:0.6b`` truth.
  * Where langchain IS installed, ``ChatOllama.get_num_tokens`` does NOT
    use the chat model's tokenizer either — langchain_core silently
    substitutes its GPT-2 fallback (which warns "Token counts may be
    inaccurate"), a THIRD unit that over-counted qwen3-embedding by
    16–87 % and made chunk boundaries depend on whether an optional
    package happened to be installed.
  * The chat-vs-embedding tokenizer divergence D16's premise named (both
    Qwen-family tokenizers) is small: 1.2–5.4 %.
  * The DOMINANT defect was the budget, not the counter: the xlarge tier
    max (13 500) exceeds qwen3-embedding's num_ctx (10 240) by 32 %. A
    chunk packed to that max measured 13 697 true tokens; Ollama embedded
    only the first 10 239 (``prompt_eval_count`` pins at the window with
    HTTP 200 — silent loss of 25 % of the chunk at index time).

Pins (red-proofed against pre-fix source):
  1. ``TestBudgetDerivesFromEmbeddingWindow`` — every model's chunk
     budget fits ``int(num_ctx * (1 - margin))`` of its OWN window, and
     the counting path consults no chat model at all.
  2. ``TestUnknownModelUnderFills`` — an unknown/empty model gets the
     conservative small tier, not the old large-context "safe default".
  3. ``TestNoSecondBudgetTable`` — the safety margin is declared ONCE
     (chunking.py; query_enrichment reads it) and ``MODEL_TOKEN_LIMITS``
     stays the only per-model token-budget dict in the tree.

R39/R40 round (2026-09-04, mid-flight user constraints — red-proofed
against the first D16 pass, which clamped to capacity only):
  4. ``TestR39PolicyCeiling`` — the ~8k chunk ceiling is a NAMED
     retrieval-quality policy BELOW the model's window (never derived
     from it); the window clamps DOWNWARD only:
     ``effective = min(policy, capacity)``. The first pass returned
     qwen3 max 9 216 (capacity-derived) — R39 pins it to the 8 192 policy.
  5. ``TestR40NumCtxExplicit`` — every Ollama embed request sets num_ctx
     explicitly, resolved from MODEL_TOKEN_LIMITS on BOTH the canonical
     adapter and the MCP inline fallback. R40 RESOLVED (2026-09-04) keeps
     qwen3 at ``num_ctx=10240`` while the chunk policy caps at 8 192 units:
     our token counter under-counts against the real tokenizer by 1.5-18%,
     so an 8 192-unit chunk is really ~8 320+ true tokens and a window set
     equal to the budget would truncate the largest chunks. The gap is
     deliberate headroom, and it costs nothing — num_ctx is a per-request
     upper bound, not an allocation.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

chunking = importlib.import_module("claude_mcp_servers.weaviate_mcp.chunking")

CHUNKING_PATH = (
    REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "chunking.py"
)
QUERY_ENRICHMENT_PATH = REPO_ROOT / "vco_lib" / "query_enrichment.py"
EMBEDDINGS_PATH = (
    REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "embeddings.py"
)
OLLAMA_PROVIDER_PATH = REPO_ROOT / "vco_lib" / "embedding_providers" / "ollama.py"


class TestBudgetDerivesFromEmbeddingWindow:
    def test_every_models_budget_fits_its_own_window(self) -> None:
        margin = chunking._BUDGET_SAFETY_MARGIN_RATIO
        for model, num_ctx in chunking.MODEL_TOKEN_LIMITS.items():
            _min, max_t, target_t = chunking.chunking_preset_for_model(model)
            budget = int(num_ctx * (1 - margin))
            assert max_t <= budget, (
                f"{model}: chunk max {max_t} exceeds its own window budget "
                f"{budget} (num_ctx {num_ctx} x (1 - {margin})) — R29: the "
                "budget is the EMBEDDING model's input window, never above it"
            )
            assert target_t <= budget, (f"{model}: target over window budget",)
            assert _min <= max_t

    def test_qwen3_xlarge_no_longer_exceeds_its_window(self) -> None:
        """RED-PROOF target: pre-fix max was the xlarge tier's 13 500 —
        32% ABOVE qwen3-embedding's 10 240 num_ctx, the overflow that
        silently truncated max-packed chunks by ~25% at embed time.
        R39 (2026-09-04): the binding constraint is the ~8k RETRIEVAL-
        QUALITY policy, which sits BELOW the window budget — the ceiling
        is min(policy, capacity), never the capacity itself."""
        _min, max_t, _target = chunking.chunking_preset_for_model(
            "qwen3-embedding:0.6b"
        )
        num_ctx = chunking.MODEL_TOKEN_LIMITS["qwen3-embedding:0.6b"]
        capacity_budget = int(num_ctx * (1 - chunking._BUDGET_SAFETY_MARGIN_RATIO))
        assert max_t == min(
            chunking.CHUNK_TOKEN_POLICY_CEILING, capacity_budget
        )
        assert max_t < 13_500, "xlarge tier max must be clamped below 13 500"

    def test_fitting_tiers_keep_locked_tuples(self) -> None:
        """The clamp may not shrink tiers that already fit: a model whose
        tier max is already <= its window budget keeps the exact
        user-locked preset tuple (v0.2.47 locked design); an overflowing
        tier is clamped componentwise, never above the budget."""
        def _tier_name(num_ctx: int) -> str:
            # independent re-derivation of the tier ladder
            if num_ctx <= 512:
                return "xsmall_context"
            if num_ctx <= 2048:
                return "small_context"
            if num_ctx <= 4096:
                return "medium_context"
            if num_ctx <= 8192:
                return "large_context"
            return "xlarge_context"

        for model, num_ctx in chunking.MODEL_TOKEN_LIMITS.items():
            tier = chunking.CHUNKING_PRESETS[_tier_name(num_ctx)]
            budget = min(
                int(num_ctx * (1 - chunking._BUDGET_SAFETY_MARGIN_RATIO)),
                chunking.CHUNK_TOKEN_POLICY_CEILING,
            )
            resolved = chunking.chunking_preset_for_model(model)
            if tier[1] <= budget:
                assert resolved == tier, (
                    f"{model}: tier {tier} already fits budget {budget} but "
                    f"resolver returned {resolved}"
                )
            else:
                assert resolved == (
                    min(tier[0], budget), min(tier[1], budget), min(tier[2], budget)
                ), f"{model}: overflowing tier {tier} must clamp to {budget}"

    def test_counting_path_consults_no_chat_model(self) -> None:
        """RED-PROOF target: pre-fix chunking.py read ``$TOKENIZER_MODEL``
        (default qwen3.5:0.8b — a CHAT model) and constructed a ChatOllama
        for counting. Post-fix the counter is pure chars arithmetic."""
        src = CHUNKING_PATH.read_text(encoding="utf-8")
        assert "TOKENIZER_MODEL" not in src, (
            "the chunk counter must not read TOKENIZER_MODEL — a chat-model "
            "tokenizer is the wrong unit for an embedding-window budget (D16)"
        )
        assert "ChatOllama" not in src, (
            "the chunk counter must not construct a ChatOllama — langchain "
            "silently backs get_num_tokens with a GPT-2 fallback tokenizer, "
            "a third unit that over-counts qwen3-embedding by up to 87%"
        )

    def test_count_is_deterministic_chars_arithmetic(self) -> None:
        """ONE unit, no hidden state, no install-dependent tokenizer: the
        count is ``len(text) // CHARS_PER_TOKEN_TEXT`` for every input."""
        assert not hasattr(chunking.TokenCounter, "_llm")
        assert not hasattr(chunking.TokenCounter, "_use_approximation")
        cases = ["", "hello world", "def f(): pass", "perché é", "x" * 9_999]
        for text in cases:
            assert chunking.TokenCounter.count_tokens(text) == (
                len(text) // chunking.CHARS_PER_TOKEN_TEXT
            )


class TestUnknownModelUnderFills:
    def test_unknown_model_gets_conservative_small_tier(self) -> None:
        """RED-PROOF target: pre-fix unknown -> large_context (max 6 400 —
        over-fills any unknown model with a <8k-class real window). The
        conservative direction is the tightest general tier (same call the
        sibling R29 fix made in query_enrichment._resolve_budget)."""
        preset = chunking.chunking_preset_for_model("definitely-not-registered:9b")
        assert preset == chunking.CHUNKING_PRESETS["small_context"], (
            f"unknown model routed to {preset}; the fallback must UNDER-fill "
            "(small_context), never guess large"
        )

    def test_empty_model_list_under_fills(self) -> None:
        assert chunking.chunking_preset_for_models([]) == \
            chunking.CHUNKING_PRESETS["small_context"]

    def test_known_small_window_models_keep_their_own_tier(self) -> None:
        # The conservative unknown default never widens a KNOWN model's own
        # tighter tier: granite (512 ctx) keeps xsmall, byte-identical.
        assert chunking.chunking_preset_for_model(
            "granite-embedding:278m-fp16"
        ) == chunking.CHUNKING_PRESETS["xsmall_context"]

    def test_singleton_multi_matches_single_model_resolver_for_unknown(self) -> None:
        """The documented singleton contract (``for_models([x]) ==
        for_model(x)``) must hold for unknown x too, not just known ones."""
        assert chunking.chunking_preset_for_models(["no-such-model"]) == \
            chunking.chunking_preset_for_model("no-such-model")


class TestNoSecondBudgetTable:
    def test_safety_margin_is_declared_once(self) -> None:
        """RED-PROOF target: the num_ctx safety margin must live ONCE in
        chunking.py (the budget SSOT). query_enrichment used to declare its
        own private ``_BUDGET_SAFETY_MARGIN_RATIO = 0.1`` — exactly the
        drifted-duplicate shape that produced the code_truncation defect
        (jina pinned 8 192 against an SSOT of 2 048)."""
        tree = ast.parse(CHUNKING_PATH.read_text(encoding="utf-8"))
        # Module-level declaration, plain OR annotated (``x = 0.1`` /
        # ``x: float = 0.1``) — the declaration FORM is incidental, the
        # once-ness and the literal are the contract.
        decls = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
                value = node.value
            else:
                continue
            if "_BUDGET_SAFETY_MARGIN_RATIO" in names:
                decls.append(value)
        assert len(decls) == 1, "chunking.py must declare the margin exactly once"
        assert isinstance(decls[0], ast.Constant)

    def test_query_enrichment_reads_the_home_not_a_literal(self) -> None:
        tree = ast.parse(QUERY_ENRICHMENT_PATH.read_text(encoding="utf-8"))
        literals = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
                value = node.value
            else:
                continue
            if any("SAFETY" in n for n in names) and isinstance(value, ast.Constant):
                literals.append(node)
        assert literals == [], (
            "query_enrichment re-declares a safety-margin literal — the "
            "margin must be imported from chunking (one home)"
        )
        query_enrichment = importlib.import_module("vco_lib.query_enrichment")
        assert (
            query_enrichment._BUDGET_SAFETY_MARGIN_RATIO
            == chunking._BUDGET_SAFETY_MARGIN_RATIO
        )

    def test_model_token_limits_is_still_the_only_budget_dict(self) -> None:
        """No per-model token-budget DICT may appear outside chunking.py —
        a second table is how the code_truncation drift happened. (Scalar
        fallbacks like code_truncation._DEFAULT_TOKEN_LIMIT are constants
        for a char budget, not model->token tables; dict values only.)"""
        offenders = []
        for base in (REPO_ROOT / "claude_mcp_servers", REPO_ROOT / "vco_lib"):
            for py in base.rglob("*.py"):
                if "__pycache__" in py.parts:
                    continue
                tree = ast.parse(py.read_text(encoding="utf-8"))
                for node in tree.body:
                    if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                        continue
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and "TOKEN_LIMIT" in tgt.id:
                            if py.resolve() != CHUNKING_PATH.resolve():
                                offenders.append(f"{py}:{tgt.id}")
        assert offenders == [], (
            f"second per-model token-budget table(s) declared outside "
            f"chunking.py: {offenders}"
        )


class TestR39PolicyCeiling:
    """R39 (2026-09-04): the ~8k chunk ceiling is a RETRIEVAL-QUALITY policy,
    deliberately BELOW the embedding model's context window and NOT derived
    from it. An oversized chunk matches on a fragment and then returns the
    whole thing — partial matches retrieve massive, mostly-irrelevant
    results that crowd the answer window. The gap between this ceiling and
    the model's window is load-bearing; the constant must carry its reason
    AT the number so the next optimiser cannot read it as an inconsistency
    to close."""

    def test_policy_ceiling_is_a_named_constant_with_reason_attached(self) -> None:
        ceiling = chunking.CHUNK_TOKEN_POLICY_CEILING  # AttributeError pre-fix
        assert ceiling == 8_192
        src = CHUNKING_PATH.read_text(encoding="utf-8")
        i = src.index("CHUNK_TOKEN_POLICY_CEILING")
        preamble = src[max(0, i - 2_000):i].lower()
        assert "r39" in preamble and "retrieval" in preamble, (
            "the R39 reason must be attached AT the constant — a bare 8_192 "
            "reads as a capacity artifact and gets 'optimised' upward"
        )

    def test_every_models_chunk_max_respects_the_policy_ceiling(self) -> None:
        ceiling = chunking.CHUNK_TOKEN_POLICY_CEILING
        for model in chunking.MODEL_TOKEN_LIMITS:
            _min, max_t, target_t = chunking.chunking_preset_for_model(model)
            assert max_t <= ceiling, (
                f"{model}: chunk max {max_t} exceeds the R39 policy ceiling "
                f"{ceiling} — retrieval-quality cap, not capacity"
            )
            assert target_t <= ceiling

    def test_qwen3_ceiling_is_the_policy_not_the_capacity(self) -> None:
        """The undo-test for the capacity-derived clamp: qwen3's window
        budget int(10 240 * 0.9) = 9 216 sits ABOVE the policy — when the
        window is wider than the policy allows, the POLICY binds, never the
        capacity. (The first D16 pass clamped to 9 216: capacity-derived,
        exactly the shape R39 forbids.)"""
        ceiling = chunking.CHUNK_TOKEN_POLICY_CEILING
        capacity_budget = int(
            chunking.MODEL_TOKEN_LIMITS["qwen3-embedding:0.6b"]
            * (1 - chunking._BUDGET_SAFETY_MARGIN_RATIO)
        )
        assert capacity_budget > ceiling, (
            "qwen3's window budget must genuinely sit above the policy for "
            "this discriminator to bind"
        )
        _min, max_t, _target = chunking.chunking_preset_for_model(
            "qwen3-embedding:0.6b"
        )
        assert max_t == ceiling == 8_192

    def test_capacity_still_clamps_downward_below_policy(self) -> None:
        # R39: where the window legitimately enters, it clamps DOWNWARD
        # only — a model whose window budget is below the policy keeps the
        # tighter value (granite 512 -> 460; every tier except qwen3's
        # xlarge is unchanged by the policy).
        ceiling = chunking.CHUNK_TOKEN_POLICY_CEILING
        for model, num_ctx in chunking.MODEL_TOKEN_LIMITS.items():
            _min, max_t, _target = chunking.chunking_preset_for_model(model)
            budget = min(
                int(num_ctx * (1 - chunking._BUDGET_SAFETY_MARGIN_RATIO)),
                ceiling,
            )
            assert max_t <= budget, (
                f"{model}: max {max_t} above min(policy, capacity) {budget}"
            )


class TestR40NumCtxExplicit:
    """R40 (2026-09-04): every Ollama embedding request must set ``num_ctx``
    explicitly — unset means Ollama's small default and SILENT truncation of
    chunk tails (a real vector over the first fraction of the chunk; every
    downstream signal looks healthy). And the value sent must be consistent
    with the chunk budget: the canonical adapter resolves it from
    MODEL_TOKEN_LIMITS, the same table the budget clamps against."""

    def test_canonical_adapter_always_sets_num_ctx(self) -> None:
        src = OLLAMA_PROVIDER_PATH.read_text(encoding="utf-8")
        # embed() + embed_batch(): both auto-resolve None via the SSOT and
        # put it in options explicitly.
        assert src.count('"options": {"num_ctx": num_ctx}') == 3, (
            "ALL THREE Ollama adapter call sites must send an explicit, "
            "resolver-backed options.num_ctx (R40: unset = Ollama default = "
            "silent truncation). The third is the legacy `/api/embeddings` "
            "fallback, which shipped with neither num_ctx nor truncate until "
            "round 6 — it was invisible to the AST gate because that gate "
            "required the literal 'OLLAMA' in the URL expression while this "
            "adapter builds its URL from an attribute. A COUNT pinned here is "
            "deliberately brittle: adding a fourth embed site must break this "
            "test and force a decision, not slip through."
        )
        assert "_num_ctx_for_model(model)" in src

    def test_canonical_adapter_sends_the_token_limits_value(self) -> None:
        """FINDING PIN: the canonical path's num_ctx for qwen3 comes from
        the MODEL_TOKEN_LIMITS entry — 10 240, per R40 RESOLVED. The window
        deliberately EXCEEDS the 8 192-unit chunk policy: the counter that
        sizes chunks under-counts against the real tokenizer, so equality
        would truncate the largest chunks. This pin makes the adapter keep
        FOLLOWING the table rather than carrying a second copy of the
        number, so a future retune moves the sent value with it."""
        from vco_lib.embedding_providers import ollama as ollama_provider

        assert ollama_provider._num_ctx_for_model(
            "qwen3-embedding:0.6b"
        ) == chunking.MODEL_TOKEN_LIMITS["qwen3-embedding:0.6b"]

    def test_mcp_inline_text_fallback_sets_num_ctx_explicitly(self) -> None:
        """R40's demand on EVERY Ollama embed path: num_ctx must be set
        explicitly — unset means Ollama's small default and silent
        truncation.

        Updated v0.2.92 round-3. This pin used to require the LITERAL 8 192
        and recorded the resulting defect as "REPORTED, not patched": the
        canonical adapter sends 10 240 for qwen3 while this path hardcoded
        8 192, so a max-packed chunk (~8 316 true tokens) overflowed by
        ~1.5% on THIS path only. Two hardcoded windows for one model is the
        drifted-duplicate-table shape that already caused a live 4x
        over-budget bug this cycle, so the value now comes from the same
        resolver that sizes the chunks — one home, no drift. The path also
        sends `truncate: false`, so an over-window input is refused rather
        than silently losing its tail."""
        src = EMBEDDINGS_PATH.read_text(encoding="utf-8")
        fn = src.split("async def get_ollama_embedding", 1)[1]
        fn = fn.split("async def ", 1)[0]
        assert '"num_ctx": _nctx(' in fn, (
            "the inline text-embed fallback must resolve num_ctx from the one "
            "home that also sizes the chunks — a second hardcoded window for "
            "the same model is free to drift from it (R40)"
        )
        assert '"truncate": False' in fn, (
            "the fallback must refuse an over-window input rather than let "
            "Ollama silently drop its tail (qwen3 and jina truncate at HTTP "
            "200 by default; only arctic refuses)"
        )
