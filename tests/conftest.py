# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared pytest fixtures for the VCO_dev test suite.

The session-wide autouse fixture below sets ``VCT_DISABLE_HUB_RESOLVER=1``
before any test module imports. Without it, ~26 KG / access-list /
diagrams / shared-KG tests would silently pick up live ``vct-hub`` data
from the dev machine — `_try_resolve_project_config()` in
``claude_mcp_servers/weaviate_mcp/server.py`` calls the running hub and
its return value wins over the env vars that those tests inject via
``_fresh_server`` / monkeypatch.

The env-guard short-circuit lives in production code (the same function),
gated on the env var being present — production runs leave it unset
and preserve the hub-first resolution semantics.

Tests that specifically exercise the resolver path (e.g.
``test_caller_migration_step18.py``, ``test_project_resolution.py``) opt
out via a per-test or per-class hook. The opt-out list is intentionally
explicit so an accidentally-broken hub-resolver test surfaces loudly
rather than silently picking up the live machine's hub config.

See the v0.2.47 RL-6c triage report (auditor findings) for the discovery
context.
"""
from __future__ import annotations

import os

import pytest


# Test files that explicitly exercise the hub-resolver and MUST run with
# ``_try_resolve_project_config`` enabled. The autouse fixture below
# leaves the env var UNSET when the file under test is in this list, so
# `mock.patch("vco_lib.project_config.resolve", ...)` calls land on the
# real code path.
_RESOLVER_OPT_OUT_FILES = frozenset({
    "test_caller_migration_step18.py",
    "test_project_resolution.py",
})


@pytest.fixture(autouse=True)
def _disable_hub_resolver_in_tests(request):
    """Force ``_try_resolve_project_config`` to fall through to env-only
    resolution for tests that DON'T explicitly exercise the resolver.

    The KG / access-list / diagrams / shared-KG cluster (~26 tests across
    6+ files) needs env-only resolution to keep their injected env vars
    intact. The resolver-test cluster (`test_caller_migration_step18.py`,
    `test_project_resolution.py`) needs the resolver enabled so their
    ``mock.patch("vco_lib.project_config.resolve", ...)`` calls have an
    effect. We discriminate by test file name; the opt-out list above is
    the canonical record of "tests that test the hub-resolver itself".
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
