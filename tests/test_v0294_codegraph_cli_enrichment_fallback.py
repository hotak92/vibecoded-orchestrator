# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.94 — the no-EmbeddingService branch of ``search_by_concept`` was dead.

``templates/scripts/query_code_graph.py`` (shipped as every project's
``.claude/scripts/`` code-graph CLI, reached through the
``code-graph-query`` wrapper) resolves the model whose token budget the
WP-E query-enrichment step must respect::

    _svc = _get_or_create_embedding_service()
    _code_model = _svc.code_model_id if _svc is not None else DEFAULT_CODE_MODEL

``DEFAULT_CODE_MODEL`` was never imported into that module — ruff F821. The
name is only evaluated when ``_svc is None``, which is exactly the situation
the fallback exists for:

  * ``HAS_EMBEDDING_SERVICE`` is False (lean / broken install), or
  * ``EmbeddingService.for_project()`` raised — no embedding backend
    reachable (Ollama down, code-embed service down): the ordinary field
    failure this branch was written to survive.

The NameError did not crash the CLI: the enclosing
``except Exception`` prints ``Query enrichment skipped: …`` and carries on.
That is what kept it invisible — and what made it harmful. On that branch the
run silently lost BOTH halves of the WP-E contract: the short-query
enrichment AND the oversized-query cap, so an over-budget query went to the
embedder unclipped, with a message naming a Python identifier rather than the
real condition.

These tests drive the branch itself (no Weaviate, no live backend) and pin
that it resolves the SHARED default from ``vco_lib.embedding_service`` — the
one home for that value — rather than a literal invented at the call site.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr
from unittest.mock import patch

import pytest

# Reuse the loader that already pins $VCT_ORCHESTRATOR_ROOT while exec'ing the
# CLI (see its long comment: without the pin, importing this script can
# re-point the `claude_mcp_servers` namespace package at another clone).
from tests.test_codegraph_cli_readpath_v0270 import _load_cli_module
from vco_lib.embedding_service import DEFAULT_CODE_MODEL


@pytest.fixture(scope="module")
def cli():
    return _load_cli_module()


def _run_search(cli, service, recorder):
    """Drive search_by_concept far enough to execute the enrichment block.

    ``generate_code_embedding`` is stubbed to return nothing, so the method
    prints its "failed to generate" line and returns right after enrichment —
    no client, no Weaviate, no network.
    """
    def _fake_build_query(query, *, transcript_path=None, embedding_models=None):
        recorder["models"] = list(embedding_models or [])
        return cli.__dict__.get("_EnrichedStub", _Enriched)(query)

    q = cli.CodeGraphQuery(project="Demo")
    err = io.StringIO()
    with patch.object(cli, "_get_or_create_embedding_service", lambda: service), \
         patch.object(cli, "generate_code_embedding", lambda _text: None), \
         patch("vco_lib.query_enrichment.build_query", _fake_build_query), \
         redirect_stderr(err):
        q.search_by_concept("find the auth middleware", limit=1)
    return err.getvalue()


class _Enriched:
    """Minimal stand-in for query_enrichment.EnrichedQuery."""

    def __init__(self, text: str) -> None:
        self.text = text


def test_no_service_falls_back_to_the_shared_default_model(cli) -> None:
    """The bug: with no service the fallback name did not exist."""
    recorder: dict = {}
    stderr = _run_search(cli, service=None, recorder=recorder)

    assert "is not defined" not in stderr, (
        "the no-service fallback raised NameError and was swallowed by the "
        f"best-effort except — enrichment never ran. stderr: {stderr!r}"
    )
    assert "Query enrichment skipped" not in stderr, (
        f"enrichment must run on the no-service path, not be skipped: {stderr!r}"
    )
    assert recorder.get("models") == [DEFAULT_CODE_MODEL], (
        "the budget must be computed against the SHARED default code model "
        f"(vco_lib.embedding_service.DEFAULT_CODE_MODEL), got {recorder.get('models')!r}"
    )


def test_service_present_still_uses_its_resolved_model(cli) -> None:
    """LEAVE-ALONE: the normal path keeps naming the service's own model."""
    recorder: dict = {}

    class _Svc:
        code_model_id = "some-other-code-model"

    stderr = _run_search(cli, service=_Svc(), recorder=recorder)

    assert recorder.get("models") == ["some-other-code-model"], (
        f"resolved model must come from the service, got {recorder.get('models')!r}"
    )
    assert "is not defined" not in stderr
