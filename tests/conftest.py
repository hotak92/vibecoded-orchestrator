# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pytest fixtures shared across the orchestrator test suite.

v0.2.46 KG-AUTO-HEAL-E + v0.2.47 RL-6c (paired ship): the autouse fixture
below forces ``VCT_DISABLE_HUB_RESOLVER=1`` for every test EXCEPT those
in the opt-out list (which explicitly exercise the hub-resolver path).

**Why this is needed**: on developer machines where the launcher's
``vct-hub`` is running (this dogfood box, every contributor's local
setup), tests that monkey-patch ``KG_COLLECTION`` /
``SHARED_KG_COLLECTION`` / ``VCT_KG_ACCESS_LIST`` env vars were silently
losing to the hub-resolved values — both ``_try_resolve_project_config()``
in ``claude_mcp_servers/weaviate_mcp/server.py`` AND
``vco_lib.project_config.resolve()`` itself were called BEFORE the env-
fallback chain, and the hub returned the project's REAL bindings. Tests
passed on CI (no hub running) but failed locally with confusing diff
messages like:

    AssertionError: '[peer:Alpha]' != '[self]'
    AssertionError: 'VibeCodedOrchestrator_KnowledgeGraph' != 'AcmeTeam_SharedKG'

The gate at ``vco_lib.project_config.resolve`` short-circuits to
``HubUnreachable`` when ``VCT_DISABLE_HUB_RESOLVER`` is truthy. The
calling script's try/except then falls through to its env-var
fallback path, which is what the tests have been setting up.

The opt-out list contains tests that EXPLICITLY exercise the resolver
path (they spawn a mock hub and want the production code path to
actually reach the mocked function). The opt-out is intentionally
explicit so an accidentally-broken hub-resolver test surfaces loudly
rather than silently picking up the live machine's hub config.

References:
- ``vco_lib/project_config.py::resolve`` — the gate (v0.2.46).
- ``claude_mcp_servers/weaviate_mcp/server.py::_try_resolve_project_config``
  — the same gate at the MCP layer (v0.2.47 RL-6c).
- ``knowledge/concepts/launcher-hub-single-writer-principle.md`` — why
  the hub is the production source of truth (but tests need a hatch).
- ``knowledge/concepts/parallel-pr-coordination-gotchas-2026-05-10.md``
  §14 — the lesson cluster this conftest closes.
"""
from __future__ import annotations

import os

import pytest


# Test files that explicitly exercise the hub-resolver and MUST run with
# the gate UNSET (so `vco_lib.project_config.resolve` reaches its HTTP
# probe + their mock-patches actually fire). The autouse fixture below
# clears the env var for these files; sets it for everyone else.
_RESOLVER_OPT_OUT_FILES = frozenset({
    # Tests that mock or call resolve() directly and need the production
    # code path to NOT short-circuit:
    "test_caller_migration_step18.py",
    "test_project_resolution.py",
    "test_project_config.py",       # v0.2.46: also exercises resolve() directly
})


@pytest.fixture(autouse=True)
def _disable_hub_resolver_in_tests(request):
    """Force ``_try_resolve_project_config`` to fall through to env-only
    resolution for tests that DON'T explicitly exercise the resolver.

    The KG / access-list / diagrams / shared-KG cluster (~26 tests across
    6+ files) needs env-only resolution to keep their injected env vars
    intact. The resolver-test cluster (`test_caller_migration_step18.py`,
    `test_project_resolution.py`, `test_project_config.py`) needs the
    resolver enabled so their ``mock.patch("vco_lib.project_config.
    resolve", ...)`` calls have an effect. We discriminate by test file
    name; the opt-out list above is the canonical record of "tests that
    test the hub-resolver itself".

    The fixture restores the prior env state in its finally block (so a
    test that sets the var itself isn't broken by this fixture).
    """
    test_file = request.node.fspath.basename
    if test_file in _RESOLVER_OPT_OUT_FILES:
        # Resolver tests: ensure the env var is NOT set so the production
        # code's guard doesn't short-circuit.
        prev = os.environ.pop("VCT_DISABLE_HUB_RESOLVER", None)
        try:
            yield
        finally:
            if prev is not None:
                os.environ["VCT_DISABLE_HUB_RESOLVER"] = prev
    else:
        # Default path: env-only resolution. Set the var if it wasn't
        # already; restore the prior value (which may be None) afterward.
        prev = os.environ.get("VCT_DISABLE_HUB_RESOLVER")
        os.environ["VCT_DISABLE_HUB_RESOLVER"] = "1"
        try:
            yield
        finally:
            if prev is None:
                os.environ.pop("VCT_DISABLE_HUB_RESOLVER", None)
            else:
                os.environ["VCT_DISABLE_HUB_RESOLVER"] = prev
