"""
Unit tests for _collapse_to_one_per_node — the chunk-collapse fix that runs
between Weaviate retrieval and the RL rerank in hybrid_search /
semantic_graph_search.

Why these tests matter beyond the user-visible duplicates fix: retrieval_rl.py
keys EVERY citation/reward/training-target dict on title alone (see e.g.
signed[title] = sim if cited else -sim). Two chunks of the same node entering
the candidate pool would silently collide there, corrupting both online
training updates and offline JSONL logs. The collapse step gives the RL server
a clean, one-record-per-node candidate pool.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "claude_mcp_servers"))

from weaviate_mcp.server import _collapse_to_one_per_node


def test_unchunked_nodes_pass_through_untouched():
    """Single-chunk / unchunked nodes are not affected."""
    inp = [
        {"title": "A", "file_path": "knowledge/a.md", "combined_score": 0.9, "chunk_number": None},
        {"title": "B", "file_path": "knowledge/b.md", "combined_score": 0.7, "chunk_number": None},
    ]
    out = _collapse_to_one_per_node(inp)
    assert len(out) == 2
    titles = [r["title"] for r in out]
    assert titles == ["A", "B"]  # already sorted desc by score
    for r in out:
        assert r["chunks_matched"] == 1


def test_two_chunks_of_same_node_collapse_to_best_chunk():
    """The motivating bug: two chunks of one node both surviving into top-K."""
    inp = [
        {"title": "Vec DB", "file_path": "knowledge/vec.md", "combined_score": 0.9, "chunk_number": 3, "content": "chunk 3 body"},
        {"title": "Vec DB", "file_path": "knowledge/vec.md", "combined_score": 0.85, "chunk_number": 7, "content": "chunk 7 body"},
        {"title": "Other", "file_path": "knowledge/other.md", "combined_score": 0.6, "chunk_number": None, "content": "other"},
    ]
    out = _collapse_to_one_per_node(inp)
    assert len(out) == 2
    vec = next(r for r in out if r["title"] == "Vec DB")
    assert vec["chunks_matched"] == 2
    assert vec["best_chunk_number"] == 3   # chunk 3 won (higher score)
    assert vec["content"] == "chunk 3 body"
    assert vec["combined_score"] == 0.9


def test_three_or_more_chunks_count_correctly():
    inp = [
        {"title": "X", "file_path": "x.md", "combined_score": 0.5, "chunk_number": 1},
        {"title": "X", "file_path": "x.md", "combined_score": 0.7, "chunk_number": 2},
        {"title": "X", "file_path": "x.md", "combined_score": 0.6, "chunk_number": 3},
        {"title": "X", "file_path": "x.md", "combined_score": 0.4, "chunk_number": 4},
    ]
    out = _collapse_to_one_per_node(inp)
    assert len(out) == 1
    assert out[0]["chunks_matched"] == 4
    assert out[0]["best_chunk_number"] == 2  # 0.7 was the winner


def test_file_path_disambiguates_cross_collection_title_collision():
    """Two collections with same node title (e.g. shared-KG vs project-KG)
    should NOT be collapsed — they are genuinely different nodes."""
    inp = [
        {"title": "Common Concept", "file_path": "knowledge/shared/x.md", "combined_score": 0.8, "chunk_number": None},
        {"title": "Common Concept", "file_path": "knowledge/project/x.md", "combined_score": 0.7, "chunk_number": None},
    ]
    out = _collapse_to_one_per_node(inp)
    assert len(out) == 2  # NOT collapsed
    for r in out:
        assert r["chunks_matched"] == 1


def test_missing_file_path_falls_back_to_empty_string_for_keying():
    """Defensive: nodes without file_path still get keyed (on '', title) and
    therefore collapse on title alone — that's the most common case for
    code-graph candidates which may not carry a markdown path."""
    inp = [
        {"title": "Foo", "combined_score": 0.9, "chunk_number": 1},
        {"title": "Foo", "combined_score": 0.6, "chunk_number": 2},
    ]
    out = _collapse_to_one_per_node(inp)
    assert len(out) == 1
    assert out[0]["chunks_matched"] == 2


def test_results_sorted_desc_by_score_after_collapse():
    inp = [
        {"title": "Low", "file_path": "low.md", "combined_score": 0.1, "chunk_number": None},
        {"title": "High", "file_path": "high.md", "combined_score": 0.9, "chunk_number": 1},
        {"title": "High", "file_path": "high.md", "combined_score": 0.8, "chunk_number": 2},
        {"title": "Mid", "file_path": "mid.md", "combined_score": 0.5, "chunk_number": None},
    ]
    out = _collapse_to_one_per_node(inp)
    assert [r["title"] for r in out] == ["High", "Mid", "Low"]


def test_score_field_parameter_supports_semantic_graph_search():
    """semantic_graph_search uses 'score' (1 - distance) instead of
    'combined_score'. The score_field parameter must let it score by 'score'."""
    inp = [
        {"title": "A", "file_path": "a.md", "score": 0.9, "chunk_number": 1},
        {"title": "A", "file_path": "a.md", "score": 0.6, "chunk_number": 2},
    ]
    out = _collapse_to_one_per_node(inp, score_field="score")
    assert len(out) == 1
    assert out[0]["best_chunk_number"] == 1


def test_caller_dict_not_mutated():
    """Caller's input list must not be mutated — we copy before adding fields."""
    inp = [
        {"title": "A", "file_path": "a.md", "combined_score": 0.9, "chunk_number": None},
    ]
    _ = _collapse_to_one_per_node(inp)
    assert "chunks_matched" not in inp[0]
    assert "best_chunk_number" not in inp[0]


def test_empty_input_returns_empty_list():
    assert _collapse_to_one_per_node([]) == []


def test_rl_signal_invariants():
    """The collapse must preserve the invariants the RL server relies on:
      - One record per (file_path, title) → no silent title-key collision in
        retrieval_rl.py's signed[title] dict.
      - chunks_matched > 1 is an additive learning signal — both online (it
        rides through to /cache_nodes) and offline (rl_logger sees it).
      - The winning record carries the WINNING chunk's content (not chunk 1
        by accident) so citation detection (cosine vs agent output) scores
        the right text.
    """
    inp = [
        {"title": "N", "file_path": "n.md", "combined_score": 0.5, "chunk_number": 1, "content": "weak match content"},
        {"title": "N", "file_path": "n.md", "combined_score": 0.92, "chunk_number": 5, "content": "strong match content"},
        {"title": "N", "file_path": "n.md", "combined_score": 0.7, "chunk_number": 9, "content": "okay match content"},
    ]
    out = _collapse_to_one_per_node(inp)
    assert len(out) == 1
    r = out[0]
    titles_in_pool = [x["title"] for x in out]
    assert len(set(titles_in_pool)) == len(titles_in_pool)  # no title collisions in RL pool
    assert r["chunks_matched"] == 3
    assert r["content"] == "strong match content"  # winning chunk's content survives
    assert r["combined_score"] == 0.92
