# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53 NEW-4: Weaviate health endpoint SSOT parity.

The audit (cross-os-triage-2026-06-10.md) found that
``installer.rs:627`` claims ``/v1/meta`` is "the right liveness probe",
but install.py + every other consumer uses ``/v1/.well-known/ready``
(Weaviate's documented readiness endpoint). This file LOCKS IN the
Python side as the canonical SSOT.

Track C will remove the divergent ``installer.rs:627`` comment in this
release; this test exists so the canonical value can't silently drift
on the Python side in a future change.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"

CANONICAL_HEALTH_PATH = "/v1/.well-known/ready"


def test_install_py_uses_canonical_health_endpoint_only():
    """install.py must use ``/v1/.well-known/ready`` for Weaviate health probes.

    The two acceptable patterns:
      1. Direct readiness probes (`_wait_for_weaviate`, `_seed_weaviate`).
      2. The bootstrap envelope ``weaviate_endpoints.health`` field.

    Other endpoints (`/v1/meta`, `/v1/schema`, `/v1/graphql`) are valid
    for their own purposes, but NONE may be used as a "health probe"
    substitute.
    """
    text = INSTALL_PY.read_text(encoding="utf-8")
    # Every occurrence of `.well-known/ready` is well-formed.
    assert text.count(".well-known/ready") >= 3, (
        "install.py should have at least 3 Weaviate health-probe sites "
        "(seed_weaviate, post-Weaviate-up probe, bootstrap envelope)."
    )

    # No `v1/meta` is used as a health/ready probe. Allowed for non-probe
    # uses: e.g. recording the endpoint in package_manager_advice or
    # weaviate_endpoints["meta"] — those are informational, not probes.
    # We check that no `wait_for_weaviate`/`_seed` site uses `/v1/meta`.
    health_context_re = re.compile(
        r"(wait_for_weaviate|seed_weaviate|weaviate_ready|weaviate_health)"
        r".{0,400}/v1/meta",
        re.DOTALL,
    )
    matches = health_context_re.findall(text)
    assert not matches, (
        f"install.py uses /v1/meta within a Weaviate-health context: "
        f"{matches!r}. NEW-4 SSOT violation."
    )


def test_bootstrap_envelope_publishes_canonical_health_endpoint():
    """Bootstrap envelope's `weaviate_endpoints.health` is canonical."""
    text = INSTALL_PY.read_text(encoding="utf-8")
    # _bootstrap_build_envelope sets the literal endpoint.
    assert (
        '"health": "http://localhost:8081/v1/.well-known/ready"' in text
        or "'health': 'http://localhost:8081/v1/.well-known/ready'" in text
    ), (
        "Bootstrap envelope MUST publish "
        "http://localhost:8081/v1/.well-known/ready as the canonical "
        "Weaviate health endpoint. See NEW-4 in "
        "docs/INSTALL_ARCHITECTURE_v2.md §3.4."
    )


def test_schema_documents_canonical_health_endpoint():
    """The JSON Schema file documents the canonical endpoint."""
    schema_path = (
        REPO_ROOT / "docs" / "schemas" / "install-bootstrap-envelope-v1.json"
    )
    text = schema_path.read_text(encoding="utf-8")
    assert "/v1/.well-known/ready" in text, (
        "Schema must mention the canonical endpoint by name."
    )
