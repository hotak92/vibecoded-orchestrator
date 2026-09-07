#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regenerate vco_lib/schema_versions.json from vco_lib/schema_versions.py.

The JSON is the committed, machine-readable snapshot of the Python constants.
Two things read it, and both are gates:

* ``tests/test_v52_ag_schema_versions.py`` (Python) — asserts the file matches
  what this script would produce right now, and that ``--check`` agrees.
* ``launcher/src-tauri/tests/schema_versions_rust_parity.rs`` (Rust) —
  ``include_str!``s it AT COMPILE TIME and asserts
  ``canonical_versions.launcher_db_table_set`` equals the highest version in
  ``migrations::MIGRATIONS``, so a Python-side bump that forgets its Rust
  migration (or a Rust migration that forgets the constant) is a `cargo test`
  failure, not only a `pytest` one.

Run this script whenever ``vco_lib/schema_versions.py`` changes.

v0.2.92 (R16/R23): the previous docstring named
``tests/test_schema_versions_parity.py`` — a file that has never existed —
and described a Rust ``include_str!`` consumer that did not exist either. The
test name is corrected here and the Rust consumer is now real; a printed
instruction is shipped code and is reviewed as code.

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
            "tests/test_v52_ag_schema_versions.py (Python) and "
            "launcher/src-tauri/tests/schema_versions_rust_parity.rs (Rust, "
            "include_str! at compile time)."
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
