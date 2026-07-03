# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A-8 (v0.2.73): Rust ↔ Python canonical env-key parity guard.

Two canonical env-key lists must stay in agreement:
  * Rust ``CANONICAL_INSTALL_ENV_KEYS`` (launcher/src-tauri/src/commands/
    projects_v2.rs) — the keys the launcher GUI writes + unregisters.
  * Python ``vco_lib.config_projection._CANONICAL_KEYS`` — the keys the
    ``apply_project_env`` contract rebuilds the managed block from.

Pre-A-8 they disagreed on ``KG_BASE_DIR``: Rust-only. The Python apply
rebuilds the managed block from scratch and drops keys not in its set, so
KG_BASE_DIR appeared after a Rust secrets-toggle write and VANISHED on the
next Python apply — a flapping surface. A-8 added KG_BASE_DIR to the Python
set.

This test asserts ``RUST_KEYS ⊆ PYTHON_KEYS`` (every Rust-emitted key is also
Python-canonical, so no key flaps out of existence on a Python apply). The
reverse direction (Python-only keys) is DELIBERATE and documented — the
Python ``apply`` CLI is the canonical writer per the Option-A interop
strategy (DIAGRAMS_COLLECTION, DUAL_*, VCO_CODE_GRAPH_*_FLOOR, VCT_INSTALL_ROOT,
VCT_PROJECT_ID, VCT_DIAGRAMS_ACCESS_LIST are Python-only by design), so we do
NOT require the sets to be equal.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_RUST_SRC = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs"

from vco_lib.config_projection import list_canonical_keys  # noqa: E402


def _extract_rust_canonical_keys() -> list[str]:
    """Parse the ``CANONICAL_INSTALL_ENV_KEYS`` array literal from the Rust
    source. Returns the string-literal key names in order."""
    text = _RUST_SRC.read_text(encoding="utf-8")
    marker = "pub(crate) const CANONICAL_INSTALL_ENV_KEYS: &[&str] = &["
    start = text.index(marker) + len(marker)
    end = text.index("];", start)
    body = text[start:end]
    # Match "KEY_NAME" literals; skip // comments (comments may contain
    # quoted example strings, but those are rare and would be flagged — so
    # strip line comments first).
    no_comments = re.sub(r"//[^\n]*", "", body)
    return re.findall(r'"([A-Z0-9_]+)"', no_comments)


def test_rust_source_snapshot_present() -> None:
    assert _RUST_SRC.is_file(), f"missing Rust source at {_RUST_SRC}"


def test_rust_canonical_keys_are_subset_of_python() -> None:
    """Every Rust-emitted canonical key must be Python-canonical too, so no
    key flaps out on a Python apply (the A-8 regression)."""
    rust_keys = set(_extract_rust_canonical_keys())
    python_keys = set(list_canonical_keys())
    assert rust_keys, "failed to parse Rust CANONICAL_INSTALL_ENV_KEYS"
    missing = rust_keys - python_keys
    assert not missing, (
        "Rust-only canonical env keys are absent from the Python canonical "
        f"set — they would FLAP OUT on the next Python apply: {sorted(missing)}.\n"
        "Add each to vco_lib.config_projection._CANONICAL_KEYS with a value "
        "resolver arm (the A-8 fix pattern for KG_BASE_DIR)."
    )


def test_kg_base_dir_is_now_python_canonical() -> None:
    """Regression anchor for the specific A-8 fix."""
    assert "KG_BASE_DIR" in set(list_canonical_keys()), (
        "KG_BASE_DIR must be in the Python canonical set (A-8) so a Rust "
        "secrets-toggle write of it does not vanish on the next Python apply."
    )
    assert "KG_BASE_DIR" in set(_extract_rust_canonical_keys())
