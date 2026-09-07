# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 duplication-merge, wave-3 register #50 — the chars-per-token
heuristic lives ONCE, beside the token budgets it multiplies.

Three modules each declared their own number: ``code_truncation.py`` (3.5,
code), ``vco_lib/embedding_service.py`` (4, text) and
``vco_lib/query_enrichment.py`` (4, text — whose own comment promised to
"stay consistent" with the token counter, a promise two independent
literals cannot keep). Now ``chunking.CHARS_PER_TOKEN_TEXT`` /
``CHARS_PER_TOKEN_CODE`` are the values and the three sites read them.

Red-proofed against the pre-merge copies in ``/tmp/merge-lane/pre/r50/``:
each carried a module-level ``_CHARS_PER_TOKEN`` / ``_APPROX_CHARS_PER_TOKEN``
literal, which check 2 rejects.

Later in the same round six MORE files were routed through the shared
value (``rl_client/answer_window.py``, ``rl_client/embed_regen.py``,
``weaviate_mcp/embeddings.py``, ``weaviate_mcp/rl_state.py`` read it by
import; ``templates/scripts/search_knowledge.py`` carries ONE declared
class-C mirror). SITES now lists every consumer so a re-declaration in
any of them is caught; the mirror is exempted BY NAME and pinned by
value instead.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from claude_mcp_servers.rl_client import answer_window  # noqa: E402
from claude_mcp_servers.weaviate_mcp import (  # noqa: E402
    chunking,
    code_truncation,
    rl_state,
)
from vco_lib import embedding_service, query_enrichment  # noqa: E402

SEARCH_KNOWLEDGE = REPO_ROOT / "templates" / "scripts" / "search_knowledge.py"

SITES = (
    REPO_ROOT / "claude_mcp_servers" / "rl_client" / "answer_window.py",
    REPO_ROOT / "claude_mcp_servers" / "rl_client" / "embed_regen.py",
    REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "code_truncation.py",
    REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "embeddings.py",
    REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "rl_state.py",
    REPO_ROOT / "vco_lib" / "embedding_service.py",
    REPO_ROOT / "vco_lib" / "query_enrichment.py",
    SEARCH_KNOWLEDGE,
)

#: The ONE declared class-C mirror (a standalone-run fallback the script
#: needs because its wrapper only guarantees ``import weaviate``, not
#: ``weaviate_mcp``). Exempted BY NAME — any OTHER literal in any site,
#: or a second literal here, still fails the no-literal check.
DECLARED_MIRRORS = {
    SEARCH_KNOWLEDGE: "CHARS_PER_TOKEN_FALLBACK",
}


def test_the_two_domain_values_are_the_conservative_side():
    assert chunking.CHARS_PER_TOKEN_CODE < chunking.CHARS_PER_TOKEN_TEXT
    assert 3.0 <= chunking.CHARS_PER_TOKEN_CODE <= 4.0
    assert 3.5 <= chunking.CHARS_PER_TOKEN_TEXT <= 4.5


def test_no_site_declares_its_own_numeric_literal():
    """No module-level ``*CHARS_PER_TOKEN* = <number>`` outside chunking.py
    (AST — a docstring that explains the history does not count), except
    the one declared mirror above."""
    for path in SITES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and "CHARS_PER_TOKEN" in tgt.id:
                        if DECLARED_MIRRORS.get(path) == tgt.id:
                            continue
                        assert not isinstance(node.value, ast.Constant), (
                            f"{path.name} re-declares {tgt.id} as a literal"
                        )


def test_the_sites_read_the_home():
    assert code_truncation._CHARS_PER_TOKEN == chunking.CHARS_PER_TOKEN_CODE
    assert embedding_service._chars_per_token_text() == chunking.CHARS_PER_TOKEN_TEXT
    assert query_enrichment._approx_chars_per_token() == chunking.CHARS_PER_TOKEN_TEXT
    assert embedding_service._CHARS_PER_TOKEN == chunking.CHARS_PER_TOKEN_TEXT  # PEP 562 alias
    assert code_truncation._max_chars_for_model("jina-embeddings-v2-base-code") == int(
        chunking._num_ctx_for_model("jina-embeddings-v2-base-code") * chunking.CHARS_PER_TOKEN_CODE
    )
    # The wave-later importers (register #50 extension).
    assert answer_window._chars_per_token_text() == chunking.CHARS_PER_TOKEN_TEXT
    assert rl_state._RL_MONITOR_ANSWER_THRESHOLD == (
        rl_state._RL_MONITOR_ANSWER_THRESHOLD_TOKENS * chunking.CHARS_PER_TOKEN_TEXT
    )
    assert rl_state._RL_MIN_ANSWER_CHARS_FOR_CITATION == (
        rl_state._RL_MIN_ANSWER_TOKENS_FOR_CITATION * chunking.CHARS_PER_TOKEN_TEXT
    )


def test_search_knowledge_fallback_mirror_matches_the_home():
    """The declared class-C mirror's parity pin. The script ships into every
    project and its wrapper only guarantees ``import weaviate``, so the
    fallback literal is legitimate — but it carries a "MUST MATCH" comment,
    and a mirror without a parity test is a promise nothing enforces. Pin
    the VALUE (AST-extracted — importing the shipped script would run its
    module-level probe code) and the declaration comment."""
    tree = ast.parse(SEARCH_KNOWLEDGE.read_text(encoding="utf-8"))
    found = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "CHARS_PER_TOKEN_FALLBACK":
                    assert isinstance(node.value, ast.Constant), (
                        "the mirror must stay a plain literal for the AST pin"
                    )
                    found = node.value.value
    assert found is not None, (
        "CHARS_PER_TOKEN_FALLBACK disappeared — the standalone fallback "
        "needs its declared mirror"
    )
    assert found == chunking.CHARS_PER_TOKEN_TEXT, (
        f"CHARS_PER_TOKEN_FALLBACK={found!r} drifted from "
        f"chunking.CHARS_PER_TOKEN_TEXT={chunking.CHARS_PER_TOKEN_TEXT!r} "
        "(declared class-C mirror — MUST MATCH)"
    )
    assert "MUST MATCH" in SEARCH_KNOWLEDGE.read_text(encoding="utf-8")

