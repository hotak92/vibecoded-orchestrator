# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 (WP-3, PLAN-v0292-REMAINING §J4/§I3) -- chunk_plan parity.

``scripts/build_shipped_kg_embeddings.py::chunk_plan`` used to carry a
docstring claiming it "mirrors" two server-bound wrapper functions in the
sync script. Reading the actual body showed it never re-implemented their
logic at all -- it called ``chunking_preset_for_model`` /
``Chunker.for_model`` (the class-A shared primitives) directly. The
docstring was an unenforced parity CLAIM with nothing behind it; this
module is the enforced parity TEST that replaces it (CLAUDE.md's A>B>C
rule: a claim with no test is worse than no claim, per
PLAN-v0292-REMAINING-2026-09-02.md's own instruction for this package).

W8 (v0.2.92 wiring audit) went one step further: ``chunk_plan`` now calls
``weaviate_mcp.kg_chunk_plan.plan_node_chunks`` -- the SAME one computation
the MCP store, kg-sync and the ``--rechunk`` comparison call -- so it is
shared code, not a third producer of the same plan. The assertions below
still derive the expectation from the primitives DIRECTLY, so the
delegation cannot silently change the answer.

For a representative corpus -- under-budget single chunk, over-budget
multi-chunk, and a "class variant" (identical content, two model ids that
resolve to *different* preset tiers) -- ``chunk_plan(content, model_id)``
must equal calling ``chunking_preset_for_model`` / ``TokenCounter.
count_tokens`` / ``Chunker.for_model(...).chunk_text(...)`` directly.
Byte-identical, not approximately equal: this is a wire format in this
repo's history (PLAN-v0292-REMAINING's own acceptance text) -- any drift
here would silently desync the shipped-KG embedding sidecars from what a
real ingest against the sync script's chunker produces.

Token counting is ``len(text) // CHARS_PER_TOKEN_TEXT`` -- ONE
deterministic character-arithmetic unit since D16 (v0.2.92), so this suite
needs no forcing flags to be deterministic and network-free regardless of
whether a local Ollama daemon happens to be reachable -- pure
repo-introspection + unit tests, no network / Ollama / Weaviate, same
contract as ``test_v0289_shipped_kg_embeddings.py``.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR_PATH = REPO_ROOT / "scripts" / "build_shipped_kg_embeddings.py"

# weaviate_mcp may ALSO be editable-installed by a sibling checkout's own
# venv (e.g. a dev fork on the same machine) -- if that happens to be first
# on sys.path, a bare `import weaviate_mcp` silently binds to the WRONG
# repo's copy instead of this one's claude_mcp_servers/weaviate_mcp. Insert
# THIS repo's copy at position 0 before the import, matching the exact
# convention every other test in this suite that imports weaviate_mcp
# directly already follows (see e.g.
# test_code_canonical_chunk_resolution.py, test_code_truncation_chunking.py)
# so `weaviate_mcp.chunking.__file__` below is provably this repo's file,
# and the TokenCounter chunk_plan's own internal calls use is the SAME
# class this import establishes (both resolve through the one sys.modules
# entry it creates).
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))

from weaviate_mcp.chunking import Chunker, TokenCounter, chunking_preset_for_model  # noqa: E402

_CHUNKING_FILE = Path(sys.modules["weaviate_mcp.chunking"].__file__).resolve()
assert _CHUNKING_FILE == (REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "chunking.py").resolve(), (
    f"weaviate_mcp.chunking resolved to {_CHUNKING_FILE}, not this repo's "
    f"copy under {REPO_ROOT} -- a different checkout's editable install "
    "shadowed it (imported earlier in this process, before our sys.path "
    "insert could take effect). This test would silently validate the "
    "wrong repo's chunker; fix the import order/venv rather than relaxing "
    "this assertion."
)


def _load_generator():
    """Load scripts/build_shipped_kg_embeddings.py by path (fresh per call).

    Mirrors test_v0289_shipped_kg_embeddings.py's loader pattern (uuid-
    suffixed sys.modules name so repeated loads never collide); this suite
    doesn't need to share one cached instance across tests since loading it
    has no side effects beyond a sys.path insert of paths already present.
    """
    spec = importlib.util.spec_from_file_location(
        f"_gen_chunk_plan_parity_{uuid.uuid4().hex}", GENERATOR_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _sentences(n: int) -> str:
    """``n`` short, distinct, capital-letter-started sentences.

    Shaped for ``Chunker._split_into_sentences``'s boundary regex
    (``(?<=[.!?])\\s+(?=[A-Z])``) so real sentence-level splitting is what
    gets exercised, not the double-newline paragraph fallback.
    """
    return " ".join(
        f"Sentence number {i:05d} for chunk parity testing purposes today."
        for i in range(n)
    )


class ChunkPlanSharedParityTests(unittest.TestCase):
    """``chunk_plan`` output == calling the shared primitives directly."""

    @staticmethod
    def _expected_plan(content, model_id):
        """Re-derive the plan by calling the shared primitives directly --
        the exact contract chunk_plan is supposed to delegate to, computed
        independently of chunk_plan's own code path."""
        token_count = TokenCounter.count_tokens(content)
        _min_t, max_t, _tgt_t = chunking_preset_for_model(model_id)
        if token_count <= max_t:
            return [(1, content)]
        chunker = Chunker.for_model(model_id)
        chunks = chunker.chunk_text(
            text=content, source_id="shipped-embeddings-plan", metadata={},
        )
        return [(c.chunk_number + 1, c.content) for c in chunks]

    def test_qwen3_under_budget_is_single_chunk(self):
        gen = _load_generator()
        content = _sentences(5)
        model_id = gen.DEFAULT_SLOT_MODELS["qwen3_embed"]
        self.assertLessEqual(TokenCounter.count_tokens(content), 8_192)

        plan = gen.chunk_plan(content, model_id)

        self.assertEqual(plan, [(1, content)])
        self.assertEqual(plan, self._expected_plan(content, model_id))

    def test_qwen3_over_budget_is_multi_chunk(self):
        gen = _load_generator()
        content = _sentences(1200)  # far past 8_192 tokens at ~4 chars/tok
        model_id = gen.DEFAULT_SLOT_MODELS["qwen3_embed"]
        self.assertGreater(TokenCounter.count_tokens(content), 8_192)

        plan = gen.chunk_plan(content, model_id)

        self.assertGreater(len(plan), 1)
        self.assertEqual(plan, self._expected_plan(content, model_id))
        # chunk_num is 1-indexed and contiguous.
        self.assertEqual([n for n, _ in plan], list(range(1, len(plan) + 1)))

    def test_arctic2_under_budget_is_single_chunk(self):
        gen = _load_generator()
        content = _sentences(5)
        model_id = gen.DEFAULT_SLOT_MODELS["arctic2_embed"]
        self.assertLessEqual(TokenCounter.count_tokens(content), 3_200)

        plan = gen.chunk_plan(content, model_id)

        self.assertEqual(plan, [(1, content)])
        self.assertEqual(plan, self._expected_plan(content, model_id))

    def test_class_variant_same_content_different_preset_tiers(self):
        """Identical content: single chunk under qwen3's window-clamped
        xlarge budget (max 8_192 units -- D16+R39 v0.2.92:
        min(policy 8 192, int(10 240 * 0.9) = 9 216))
        but multi-chunk under arctic2's medium_context budget (max 3_200
        tokens) -- proves the plan is driven by the model id's preset tier,
        not a hardcoded threshold."""
        gen = _load_generator()
        content = _sentences(400)  # sized between the two models' max_t
        qwen_model = gen.DEFAULT_SLOT_MODELS["qwen3_embed"]
        arctic_model = gen.DEFAULT_SLOT_MODELS["arctic2_embed"]

        token_count = TokenCounter.count_tokens(content)
        self.assertGreater(token_count, 3_200)
        self.assertLessEqual(token_count, 8_192)

        qwen_plan = gen.chunk_plan(content, qwen_model)
        arctic_plan = gen.chunk_plan(content, arctic_model)

        self.assertEqual(qwen_plan, [(1, content)])
        self.assertGreater(len(arctic_plan), 1)

        self.assertEqual(qwen_plan, self._expected_plan(content, qwen_model))
        self.assertEqual(arctic_plan, self._expected_plan(content, arctic_model))


if __name__ == "__main__":
    unittest.main()
