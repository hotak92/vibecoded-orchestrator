# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""2026-07-14 split-brain lock — Rust ↔ Python module_settings addressing parity.

The shared-KG gate flags (and, since 2026-07-14, the shared-secrets read gate)
live in ``module_settings`` under ONE canonical ``module_id`` and a fixed set of
setting-key names. Three surfaces must agree on those strings BYTE-FOR-BYTE:

  * Rust WRITERS/READERS — the canonical home
    ``launcher/src-tauri/vct-launcher-core/src/db/module_settings_keys.rs``
    (``ORCHESTRATOR_CORE_MODULE_ID`` + the ``SETTING_KEY_*`` consts). The
    launcher setters/getters (``commands/projects_v2.rs``) and the hub
    ``/config`` resolver (``vct-hub/src/config_api.rs``) consume these.
  * Python READER — ``vco_lib/config_projection.py`` reads the SAME
    ``module_id`` + keys as string literals when projecting env from
    launcher.db.

Python has no compile-time link to the Rust consts (different language, no
subprocess in this hot path), so this test is the tier-B lock of the A>B>C
sharing rule: it parses the canonical strings out of the Rust module and
asserts the Python reader uses the identical literals. If either side is
renamed without the other, THIS test fails loudly — which is exactly what was
missing when the writers drifted to ``"__project__"`` while the readers stayed
on ``"orchestrator-core"`` (the incident this module fixes).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_RUST_KEYS_SRC = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "vct-launcher-core"
    / "src"
    / "db"
    / "module_settings_keys.rs"
)
_PY_PROJECTION_SRC = REPO_ROOT / "vco_lib" / "config_projection.py"


def _extract_rust_const(name: str) -> str:
    """Parse ``pub const <name>: &str = "<value>";`` from the Rust module."""
    text = _RUST_KEYS_SRC.read_text(encoding="utf-8")
    m = re.search(
        rf'pub const {re.escape(name)}\s*:\s*&str\s*=\s*"([^"]+)"\s*;',
        text,
    )
    assert m, f"could not parse Rust const {name} from {_RUST_KEYS_SRC}"
    return m.group(1)


def test_rust_and_python_sources_present() -> None:
    assert _RUST_KEYS_SRC.is_file(), f"missing Rust module at {_RUST_KEYS_SRC}"
    assert _PY_PROJECTION_SRC.is_file(), f"missing Python reader at {_PY_PROJECTION_SRC}"


def test_canonical_module_id_matches() -> None:
    """The Rust ``ORCHESTRATOR_CORE_MODULE_ID`` must be exactly what the Python
    projection reads (`"orchestrator-core"`)."""
    module_id = _extract_rust_const("ORCHESTRATOR_CORE_MODULE_ID")
    assert module_id == "orchestrator-core", (
        f"canonical module_id drifted in Rust: {module_id!r}"
    )
    py = _PY_PROJECTION_SRC.read_text(encoding="utf-8")
    # The Python reader must call _fetch_module_setting_bool with this id.
    assert f'"{module_id}"' in py, (
        f"config_projection.py does not use the canonical module_id "
        f"{module_id!r} — the KG-gate split-brain would re-open"
    )
    # And it must NOT read the legacy sentinel for these gate keys.
    assert '"__project__"' not in py, (
        "config_projection.py must NOT read module_id '__project__' for the "
        "gate keys — that was the split-brain sentinel"
    )


def test_gate_setting_keys_match_between_rust_and_python() -> None:
    """Each canonical setting-key literal must appear in the Python reader
    (the projection reads these exact key names from module_settings)."""
    for const_name in (
        "SETTING_KEY_SHARED_KG_WRITE_DISABLED",
        "SETTING_KEY_SHARED_KG_READ_DISABLED",
    ):
        key = _extract_rust_const(const_name)
        py = _PY_PROJECTION_SRC.read_text(encoding="utf-8")
        assert f'"{key}"' in py, (
            f"config_projection.py does not read the canonical key {key!r} "
            f"(Rust {const_name}); Rust and Python have diverged"
        )


def test_shared_secrets_read_disabled_key_is_defined() -> None:
    """The new GAP-2 secrets gate key must be defined in the shared module
    (the resolver + Tauri toggle read it; pinned here so a rename surfaces)."""
    key = _extract_rust_const("SETTING_KEY_SHARED_SECRETS_READ_DISABLED")
    assert key == "shared_secrets_read_disabled", (
        f"GAP-2 secrets-gate key drifted: {key!r}"
    )
