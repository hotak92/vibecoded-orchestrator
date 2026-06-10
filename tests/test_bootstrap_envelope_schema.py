# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Track B / v0.2.53: JSON Schema validation for the bootstrap envelope.

Runs the JSON Schema at ``docs/schemas/install-bootstrap-envelope-v1.json``
against the live envelope produced by ``install.py --bootstrap --json``.

Falls back to a structural check if ``jsonschema`` is not installed in
the test environment (jsonschema is in requirements.txt but the install
hasn't necessarily completed when this test runs in CI).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "install.py"
SCHEMA_PATH = (
    REPO_ROOT / "docs" / "schemas" / "install-bootstrap-envelope-v1.json"
)


def test_schema_file_exists():
    assert SCHEMA_PATH.is_file(), (
        f"Bootstrap envelope schema missing: {SCHEMA_PATH}"
    )


def test_schema_is_valid_json():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft-07/schema#"
    assert schema["properties"]["schema_version"]["const"] == 1


def _build_envelope() -> dict:
    env = os.environ.copy()
    env["VCT_BOOTSTRAP_TEST_MODE"] = "1"
    cp = subprocess.run(
        [sys.executable, str(INSTALL_PY), "--bootstrap", "--json"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert cp.returncode == 0, f"bootstrap exited {cp.returncode}: {cp.stderr}"
    return json.loads(cp.stdout)


def test_envelope_validates_against_schema():
    """Run jsonschema if available; else fall back to structural check."""
    envelope = _build_envelope()
    try:
        import jsonschema  # noqa: F401 — optional
    except ImportError:
        pytest.skip("jsonschema not installed; structural fallback")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    # Draft-07 validator.
    import jsonschema
    jsonschema.validate(envelope, schema)


def test_envelope_required_top_level_fields():
    """Even without jsonschema, the required fields must be present."""
    envelope = _build_envelope()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for required in schema["required"]:
        assert required in envelope, (
            f"Envelope missing required field: {required}"
        )


def test_envelope_system_required_fields():
    envelope = _build_envelope()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    sys_required = schema["properties"]["system"]["required"]
    for required in sys_required:
        assert required in envelope["system"], (
            f"Envelope missing system.{required}"
        )


def test_envelope_paths_required_fields():
    envelope = _build_envelope()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    paths_required = schema["properties"]["paths"]["required"]
    for required in paths_required:
        assert required in envelope["paths"], (
            f"Envelope missing paths.{required}"
        )


def test_envelope_weaviate_health_is_well_known_ready():
    """NEW-4: lock in the canonical Weaviate health endpoint."""
    envelope = _build_envelope()
    assert envelope["weaviate_endpoints"]["health"].endswith(
        "/v1/.well-known/ready"
    )


def test_envelope_missing_prereqs_severity_enum():
    """Each missing_prereqs entry's severity is in the schema enum."""
    envelope = _build_envelope()
    allowed = {"blocking", "warning", "optional"}
    for m in envelope["missing_prereqs"]:
        assert m["severity"] in allowed, (
            f"Invalid severity {m['severity']!r} in missing_prereqs"
        )
