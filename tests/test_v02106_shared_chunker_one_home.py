# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — the answer-window chunker lives IN the shared chunking module.

Owner ruling (2026-11-11): RL answer-window chunking must use OUR SHARED
CHUNKER (``claude_mcp_servers/weaviate_mcp/chunking.py``), sized for the model
the data is collected for. The perf lane had added a SECOND chunking home
(``rl_client/answer_chunking.py``); this file pins the merge back into the one
home:

* the KG / document chunking output is pinned BYTE-FOR-BYTE by a golden
  fixture (``tests/fixtures/kg_chunk_path_golden.json``), so
  ``_CHUNKER_REVISION`` semantics cannot drift silently;
* the answer-window entry points (``answer_chunk_budget`` /
  ``chunk_answer_window``) are importable from the shared module.

GOLDEN REGENERATED DELIBERATELY (owner ruling 2026-11-11, "fix + re-embed").
The fixture was first generated from the pre-v0.2.106 chunker, to prove the
answer-window merge left the KG path untouched. The same release then
changed the KG path on purpose: every model-aware preset is clamped to the
model's dense-content budget (``chunking.dense_char_budget``) and the packer
sizes a chunk on its joined text — so ``_CHUNKER_REVISION`` moved
``v0.2.92.1`` → ``v0.2.106`` and the fixture was regenerated from the NEW
chunker (``python tests/test_v02106_shared_chunker_one_home.py
--regenerate``). Any further change to this output is again a revision bump
plus a re-embed for every user, and must be made the same deliberate way.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_mcp_servers.weaviate_mcp import chunking

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "kg_chunk_path_golden.json"

#: Every registered model tier plus the unknown-model fallback, so the golden
#: covers every preset clamp the resolver can produce.
MODELS = [
    "snowflake-arctic-embed2:latest",
    "qwen3-embedding:0.6b",
    "text-embedding-3-small",
    "unclemusclez/jina-embeddings-v2-base-code:latest",
    "codesage/codesage-large-v2",
    "not-a-registered-model",
]


def _corpus() -> str:
    """Deterministic corpus exercising every branch of the KG split path:
    sentence packing, target/min boundaries, an oversize sentence that falls
    to the newline splitter, and a paragraph longer than every tier's hard
    char cut."""
    prose = (
        "The orchestrator stages a citation for every retrieval. "
        "Each answer window overlaps the previous one! "
        "Tool inputs are dense JSON, not prose. "
        "Boundaries must not depend on where the window starts. "
    )
    paras = []
    for i in range(40):
        paras.append(f"## Section {i}\n\n" + prose * (i % 7 + 1))
    # A >32 768-char unbroken run (no sentence boundary, no blank line)
    # forces `_split_on_newlines`' hard char cut on every tier (the widest
    # tier's cut is 8 192 tokens x 4 chars).
    paras.append("run-on " * 4700)
    # Dense tool-call JSON paragraph, also over the small tiers' cut.
    paras.append(
        'Edit: {"file_path": "/x/y.py", "old_string": "%s"}' % ("z" * 9000)
    )
    # Trailing paragraph with no sentence-final punctuation.
    paras.append("tail " * 500 + "end")
    return "\n\n".join(paras)


def _kg_path_output() -> dict:
    """Run the KG / document path over the corpus — the exact call shapes the
    KG sync and document chunkers use — and serialize everything that reaches
    Weaviate except the timestamps."""
    out: dict = {
        "chunker_revision": chunking._CHUNKER_REVISION,
        "models": {},
        "convenience": None,
    }
    for model in MODELS:
        chunker = chunking.Chunker.for_model(model)
        text_chunks = chunker.chunk_text(_corpus(), source_id=f"src::{model}")
        doc_chunks = chunker.chunk_document(
            _corpus(), document_id=f"doc::{model}", document_title=f"Title {model}"
        )
        out["models"][model] = {
            "preset": list(chunking.chunking_preset_for_model(model)),
            "chunk_text": [
                [c.content, c.chunk_number, c.total_chunks, c.token_count]
                for c in text_chunks
            ],
            "chunk_document": [
                [
                    c.content,
                    c.chunk_number,
                    c.total_chunks,
                    c.token_count,
                    c.is_first,
                    c.is_last,
                ]
                for c in doc_chunks
            ],
        }
    conv = chunking.chunk_text(_corpus(), source_id="conv", min_tokens=1000, max_tokens=2000)
    out["convenience"] = [
        [c.content, c.chunk_number, c.total_chunks, c.token_count] for c in conv
    ]
    return out


def test_kg_document_path_is_byte_identical_to_the_pregenerated_golden():
    """The KG path's output is pinned byte-for-byte to the golden, which was
    regenerated from the v0.2.106 dense-budget chunker (see the module
    docstring). A change here means ``_CHUNKER_REVISION`` semantics changed
    and every user's knowledge graph needs re-embedding — bump the revision
    and regenerate deliberately, never to make this test pass."""
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert _kg_path_output() == golden


def test_chunker_revision_matches_the_golden():
    # The hard-contract sentinel moves WITH the golden: the fixture records
    # the revision it was generated under, so a regenerated golden without a
    # revision bump (or the reverse) fails here.
    assert chunking._CHUNKER_REVISION == "v0.2.106"
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert golden["chunker_revision"] == chunking._CHUNKER_REVISION


def test_answer_window_entry_points_live_in_the_shared_module():
    # The one home: the answer-window mode is a set of entry points ON the
    # shared chunking module (budget from the same dense table + MODEL_TOKEN_LIMITS
    # window, content-defined boundaries).
    assert callable(chunking.chunk_answer_window)
    assert callable(chunking.answer_chunk_budget)
    assert chunking.chunk_answer_window("", "qwen3-embedding:0.6b") == []
    assert chunking.answer_chunk_budget("snowflake-arctic-embed2") > 0


if __name__ == "__main__":  # pragma: no cover — deliberate regeneration only
    import sys

    if sys.argv[1:] == ["--regenerate"]:
        FIXTURE.write_text(
            json.dumps(_kg_path_output(), indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"regenerated {FIXTURE}")
    else:
        print("usage: python tests/test_v02106_shared_chunker_one_home.py --regenerate")
