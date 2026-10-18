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
strategy (DUAL_*, VCO_CODE_GRAPH_*_FLOOR, VCT_INSTALL_ROOT,
VCT_PROJECT_ID are Python-only by design), so we do
NOT require the sets to be equal. (v0.2.102: DIAGRAMS_COLLECTION and
VCT_DIAGRAMS_ACCESS_LIST left the Python set with the diagrams runtime.)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_RUST_SRC = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs"

from tests.common.rust_source import strip_rust_comments  # noqa: E402
from vco_lib.config_projection import list_canonical_keys  # noqa: E402


def _extract_rust_canonical_keys() -> list[str]:
    """Parse the ``CANONICAL_INSTALL_ENV_KEYS`` array literal from the Rust
    source. Returns the string-literal key names in order.

    The array is extracted from a comment-stripped view
    (``strip_rust_comments`` — string literals kept verbatim). The previous
    inline ``re.sub(r"//[^\\n]*", ...)`` handled line comments only, so a
    block comment holding a quoted example key (``/* see "EXAMPLE_KEY" */``)
    was parsed as a LIVE key and failed the subset gate on inert prose — the
    comment/literal blindness class register-34 exists for. It also ate
    everything after a ``//`` even inside a string literal, which would have
    truncated a key value if one ever contained a slash pair.
    """
    code = "\n".join(strip_rust_comments(_RUST_SRC.read_text(encoding="utf-8")))
    marker = "pub(crate) const CANONICAL_INSTALL_ENV_KEYS: &[&str] = &["
    start = code.index(marker) + len(marker)
    end = code.index("];", start)
    body = code[start:end]
    return re.findall(r'"([A-Z0-9_]+)"', body)


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


# ─── v0.2.104 L5: the machine-local split ───────────────────────────────

_WATCHER_SRC = REPO_ROOT / "launcher" / "src-tauri" / "src" / "services" / "settings_json_watcher.rs"


def test_machine_local_keys_are_canonical() -> None:
    """Every machine-local key is a canonical key — the split partitions the
    canonical set, it never adds a key the projection does not own."""
    from vco_lib.config_projection import MACHINE_LOCAL_ENV_KEYS

    stray = set(MACHINE_LOCAL_ENV_KEYS) - set(list_canonical_keys())
    assert not stray, f"machine-local keys outside the canonical set: {sorted(stray)}"


def test_every_rust_path_key_is_machine_local() -> None:
    """The Rust canonical keys whose value is a machine path, id or endpoint
    are all in the machine-local set (the Rust list is a subset of the Python
    one; this pins the class of each shared key)."""
    from vco_lib.config_projection import MACHINE_LOCAL_ENV_KEYS

    rust = set(_extract_rust_canonical_keys())
    expected_local = {
        "KG_BASE_DIR", "VCT_ORCHESTRATOR_ROOT", "VCT_INFRASTRUCTURE_DIR",
        "WEAVIATE_URL", "WEAVIATE_PORT", "OLLAMA_URL", "OLLAMA_PORT",
        "CODE_EMBED_URL", "CODE_EMBED_PORT",
        "VCT_KG_ACCESS_LIST", "VCT_CODE_GRAPH_ACCESS_LIST",
    }
    assert expected_local <= rust
    assert expected_local <= set(MACHINE_LOCAL_ENV_KEYS)


def test_watcher_surface_names_match_the_python_surface_table() -> None:
    """The Rust settings watcher reads each watched file's env through
    ``read-env`` under a surface NAME; those names and files must be the ones
    ``config_projection._JSON_SURFACE_FILES`` serves, or a JSONC
    settings.local.json is never hashed (fail-open reload on every write)."""
    from vco_lib import config_projection as cp

    code = "\n".join(strip_rust_comments(_WATCHER_SRC.read_text(encoding="utf-8")))
    marker = "const WATCHED_SETTINGS_FILES: &[(&str, &str)] = &["
    start = code.index(marker) + len(marker)
    body = code[start:code.index("];", start)]
    pairs = re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', body)
    assert pairs, "failed to parse WATCHED_SETTINGS_FILES"
    for file_name, surface in pairs:
        rel, env_key = cp._JSON_SURFACE_FILES[surface]
        assert rel == f".claude/{file_name}" and env_key == "env", (file_name, surface)
    assert {s for _f, s in pairs} == {
        cp._SURFACE_CLAUDE_SETTINGS, cp._SURFACE_CLAUDE_SETTINGS_LOCAL,
    }
