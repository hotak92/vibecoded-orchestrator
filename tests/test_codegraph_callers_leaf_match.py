# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""WS-4 Finding 1: query_code_structure('callers', ...) must match a
fully-qualified target against the analyzer's BARE-leaf call_names.

The code-graph analyzer stores each CodeFunction's outbound calls in
``call_names`` as BARE leaf names only (verified live:
``['_log_install_event', 'strip', 'exists', ...]``). But the natural and
documented input to a ``callers`` query is the fully-qualified ``full_name``
(the MCP's own example is ``orchestrator.agents.blackboard.claim``; Rust nodes
use ``server::start_hub_server``). Pre-fix the query did
``call_names contains_any [target]`` with the dotted target verbatim, so it
could NEVER match the bare leaves and ``callers`` silently returned nothing for
every qualified input.

``_caller_match_terms`` returns ``[target, <leaf>]`` so the query resolves for
both ``module.fn`` and bare ``fn`` inputs; these tests pin that contract.
"""
from __future__ import annotations

import pytest

srv = pytest.importorskip("claude_mcp_servers.weaviate_mcp.server")


def test_dotted_python_fullname_yields_target_and_leaf() -> None:
    assert srv._caller_match_terms("install._start_services") == [
        "install._start_services",
        "_start_services",
    ]


def test_bare_name_yields_only_itself() -> None:
    # Already a leaf → no redundant second term (and no empty-string term).
    assert srv._caller_match_terms("_start_services") == ["_start_services"]


def test_rust_qualified_yields_target_and_leaf() -> None:
    assert srv._caller_match_terms("server::start_hub_server") == [
        "server::start_hub_server",
        "start_hub_server",
    ]


def test_deep_dotted_yields_last_segment_leaf() -> None:
    assert srv._caller_match_terms("orchestrator.agents.blackboard.claim") == [
        "orchestrator.agents.blackboard.claim",
        "claim",
    ]


def test_rust_impl_method_leaf_is_last_segment() -> None:
    # crate::module::Type::method → leaf is the method name.
    assert srv._caller_match_terms("crate::mod::Type::method")[-1] == "method"


def test_target_is_always_first_term() -> None:
    # The exact input must always be tried first (exact match wins).
    for t in ("a.b.c", "x::y", "lone", "pkg.mod.Class.method"):
        assert srv._caller_match_terms(t)[0] == t
