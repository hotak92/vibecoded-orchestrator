# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pytest fixtures shared across the orchestrator test suite.

v0.2.46 KG-AUTO-HEAL-E: ``disable_hub_resolver_for_tests`` autouse
fixture forces ``VCT_DISABLE_HUB_RESOLVER=1`` for every test in the
suite. Tests that explicitly want to exercise the hub-resolver path
(e.g. ``test_project_config.py``) override the env var by temporarily
unsetting it (or pop it during their own setUp).

**Why this is needed**: on developer machines where the launcher's
``vct-hub`` is running (this dogfood box, every contributor's local
setup), tests that monkey-patch ``KG_COLLECTION`` /
``SHARED_KG_COLLECTION`` / ``VCT_KG_ACCESS_LIST`` env vars were silently
losing to the hub-resolved values — because ``vco_lib.project_config.
resolve()`` was called BEFORE the env-fallback chain in scripts /
helpers, and the hub returned the project's REAL bindings. Tests
passed on CI (no hub running) but failed locally with confusing
diff messages like:

    AssertionError: '[peer:Alpha]' != '[self]'
    AssertionError: 'VibeCodedOrchestrator_KnowledgeGraph' != 'AcmeTeam_SharedKG'

The gate at ``vco_lib.project_config.resolve`` short-circuits to
``HubUnreachable`` when ``VCT_DISABLE_HUB_RESOLVER`` is truthy. The
calling script's try/except then falls through to its env-var
fallback path, which is what the tests have been setting up.

References:
- ``vco_lib/project_config.py::resolve`` — the gate.
- ``claude_mcp_servers/weaviate_mcp/server.py::_try_resolve_project_config``
  — the same gate at the MCP layer (added in v0.2.47 RL-6c).
- ``knowledge/concepts/launcher-hub-single-writer-principle.md`` — why
  the hub is the production source of truth (but tests need a hatch).
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def disable_hub_resolver_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force-disable the vct-hub resolver for every test.

    Uses ``monkeypatch`` so the env mutation is RESTORED automatically
    at test end — no leakage into adjacent tests, no cleanup boilerplate
    in each test's tearDown.

    Tests that explicitly want to exercise the resolver path (e.g.
    ``test_project_config.py::ResolveSchemaVersionTest`` which spawns
    its own mock hub) can override by calling
    ``monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)``
    inside the test body or setUp. Most tests don't need to — they
    want the env-fallback path, which is what this fixture provides.
    """
    monkeypatch.setenv("VCT_DISABLE_HUB_RESOLVER", "1")
