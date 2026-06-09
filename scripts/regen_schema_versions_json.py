#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regenerate vco_lib/schema_versions.json from vco_lib/schema_versions.py.

The JSON file is consumed by Rust at compile time (``include_str!``) to keep
the launcher's version-check helpers in sync with Python's canonical
constants. Run this script whenever ``vco_lib/schema_versions.py`` changes;
``tests/test_schema_versions_parity.py`` asserts the two stay aligned.

Usage:

    python scripts/regen_schema_versions_json.py

Exits 0 on success, 1 if the JSON drifted from what Python would produce
(useful as a pre-commit gate).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from vco_lib.schema_versions import (  # noqa: E402
    ARTIFACT_STATE_CLASSIFICATION,
    CANONICAL_VERSIONS,
)


def build_payload() -> dict[str, object]:
    """Build the canonical JSON payload from the Python constants."""
    return {
        "_comment": (
            "Generated from vco_lib/schema_versions.py — DO NOT edit by hand. "
            "Regenerate via scripts/regen_schema_versions_json.py whenever the "
            "Python module changes. Parity asserted by "
            "tests/test_schema_versions_parity.py."
        ),
        "canonical_versions": dict(sorted(CANONICAL_VERSIONS.items())),
        "state_classification": dict(sorted(ARTIFACT_STATE_CLASSIFICATION.items())),
    }


def main(argv: list[str]) -> int:
    json_path = _ROOT / "vco_lib" / "schema_versions.json"
    expected = build_payload()
    expected_text = json.dumps(expected, indent=2) + "\n"

    check_only = "--check" in argv
    if check_only:
        if not json_path.exists():
            print(f"❌ {json_path} missing — run without --check to generate.")
            return 1
        actual_text = json_path.read_text(encoding="utf-8")
        if actual_text == expected_text:
            print(f"✅ {json_path} matches Python constants.")
            return 0
        print(
            f"❌ {json_path} is OUT OF DATE relative to "
            "vco_lib/schema_versions.py.\n"
            "Run scripts/regen_schema_versions_json.py to update."
        )
        return 1

    json_path.write_text(expected_text, encoding="utf-8")
    print(f"✅ wrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
