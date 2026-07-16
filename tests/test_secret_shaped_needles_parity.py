# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity for the secret-shaped-key needle set.

The "is this env key credential-shaped?" needle set feeds several
implementations that MUST agree, or a credential-named key could leak into
``~/.claude.json`` from one registration path while another drops it (the
exact leak the denylist exists to prevent — B-3 finding v0.2.73).

v0.2.83 WP-B4 re-anchored this parity on a SINGLE SOURCE OF TRUTH:
``vco_lib/mcp_scan_rules.toml`` ([env].secret_shaped_needles). The needle
DATA now lives once in that table; the consumers read it:

  * ``vco_lib/install_mcp.py::_SECRET_SHAPED_SUBSTRINGS``  — table read
    (``mcp_scan_rules.secret_shaped_needles()``); install.py re-imports it.
  * ``launcher/src-tauri/src/mcp_registration.rs``          — table read
    (``vct_launcher_core::mcp_scan_rules::secret_shaped_needles()`` inside
    ``is_secret_shaped_env_key``).
  * ``vco_lib/secrets_audit.py::_SECRET_SHAPED_SUBSTRINGS`` — a DELIBERATE
    mirror (the secrets-audit subsystem is out of WP-B4's MCP-registration
    scope). This test keeps it locked to the same set as the table so it
    can't silently drift.

The segment-split + ``KEY``/``*_KEY`` suffix PREDICATE stays language-local
(control-flow, not data). This test also asserts that extra rule text is
present in every implementation.

A future PR that adds e.g. ``CREDENTIAL`` to the table (or to only one
mirror) trips this test loudly at CI time.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# The cross-language SSOT table.
_SCAN_RULES_TOML = _REPO / "vco_lib" / "mcp_scan_rules.toml"

# The deliberate secrets-audit mirror (kept in a separate subsystem).
_SECRETS_AUDIT_PY = _REPO / "vco_lib" / "secrets_audit.py"

# The Rust registrar (must reference the loader, not a literal needle array).
_MCP_REGISTRATION_RS = _REPO / "launcher" / "src-tauri" / "src" / "mcp_registration.rs"

# The Python MCP-rules loader source (install_mcp reads through it).
_INSTALL_MCP_PY = _REPO / "vco_lib" / "install_mcp.py"


#: The canonical needle set (asserted identical across every implementation).
_EXPECTED_NEEDLES = {"TOKEN", "SECRET", "PAT", "PASSWORD", "PASS", "AUTH"}


def _table_needles() -> set[str]:
    """Independent pure-tomllib re-parse of the SSOT table (does NOT import
    our loader — so a loader bug is caught too)."""
    data = tomllib.loads(_SCAN_RULES_TOML.read_text(encoding="utf-8"))
    return set(data["env"]["secret_shaped_needles"])


def _parse_python_substrings_literal(source_path: Path) -> set[str]:
    """Extract the string literals from a ``_SECRET_SHAPED_SUBSTRINGS = ( ... )``
    tuple literal in a Python source file (used for the deliberate
    secrets_audit.py mirror, which is still a literal)."""
    import re

    text = source_path.read_text(encoding="utf-8")
    m = re.search(
        r"_SECRET_SHAPED_SUBSTRINGS[^\n=]*=\s*\((?P<body>.*?)\)",
        text,
        re.DOTALL,
    )
    assert m is not None, f"could not find _SECRET_SHAPED_SUBSTRINGS literal in {source_path}"
    return set(re.findall(r'"([^"]+)"', m.group("body")))


def test_table_needles_match_expected():
    """The SSOT table itself carries exactly the canonical needle set."""
    assert _table_needles() == _EXPECTED_NEEDLES


def test_install_mcp_sources_needles_from_table():
    """install_mcp's runtime ``_SECRET_SHAPED_SUBSTRINGS`` equals the table.

    Read at RUNTIME (not by grep) — the value is now a table read, so the
    old literal-grep is gone. This proves the consumer actually loads the
    SSOT, not a stale copy."""
    from vco_lib import install_mcp

    assert set(install_mcp._SECRET_SHAPED_SUBSTRINGS) == _table_needles()


def test_install_mcp_does_not_hardcode_a_needle_literal():
    """install_mcp must NOT re-introduce a literal needle tuple — it reads
    the table. Guards against a future 'convenience' inline copy."""
    import re

    text = _INSTALL_MCP_PY.read_text(encoding="utf-8")
    # A literal assignment would look like `_SECRET_SHAPED_SUBSTRINGS = ("TOKEN", ...)`.
    literal = re.search(
        r"_SECRET_SHAPED_SUBSTRINGS[^\n=]*=\s*\(\s*\"",
        text,
    )
    assert literal is None, (
        "vco_lib/install_mcp.py must source _SECRET_SHAPED_SUBSTRINGS from "
        "mcp_scan_rules.secret_shaped_needles(), not a hard-coded tuple "
        "literal (WP-B4 SSOT)."
    )


def test_secrets_audit_mirror_matches_table():
    """The DELIBERATE secrets_audit.py mirror equals the table's needle set.

    secrets_audit.py belongs to a separate subsystem and keeps its literal;
    this lock ensures it can't drift from the SSOT."""
    audit = _parse_python_substrings_literal(_SECRETS_AUDIT_PY)
    assert audit == _table_needles(), (
        "vco_lib/secrets_audit.py::_SECRET_SHAPED_SUBSTRINGS drifted from "
        f"mcp_scan_rules.toml: audit={sorted(audit)} table={sorted(_table_needles())}"
    )


def test_rust_reads_needles_from_table_not_literal():
    """The Rust registrar's ``is_secret_shaped_env_key`` must read the needle
    set from the shared loader, NOT a hard-coded array. WP-B4 removed the
    ``let needles = ["TOKEN", ...]`` literal in favour of a table read."""
    text = _MCP_REGISTRATION_RS.read_text(encoding="utf-8")
    assert "mcp_scan_rules::secret_shaped_needles()" in text, (
        "mcp_registration.rs must obtain needles via "
        "vct_launcher_core::mcp_scan_rules::secret_shaped_needles() (WP-B4 SSOT)."
    )
    # And the old literal array must be gone.
    import re

    assert re.search(r"let\s+needles\s*=\s*\[\s*\"", text) is None, (
        "mcp_registration.rs still has a literal `let needles = [\"...\"]` "
        "array — it must read the table instead (WP-B4)."
    )


@pytest.mark.parametrize(
    "source_path",
    [_INSTALL_MCP_PY, _SECRETS_AUDIT_PY, _MCP_REGISTRATION_RS],
)
def test_key_extra_rule_present_in_every_impl(source_path):
    """Every implementation adds the same extra `KEY` / `*_KEY` predicate rule
    beyond the needle segments. Assert the rule text is present so a copy that
    drops it (and would then MISS `OPENAI_API_KEY` etc.) trips this test."""
    text = source_path.read_text(encoding="utf-8")
    # Python: `upper == "KEY" or upper.endswith("_KEY")`
    # Rust:   `upper == "KEY" || upper.ends_with("_KEY")`
    assert '"KEY"' in text, f"{source_path} missing exact-KEY rule"
    assert "_KEY" in text, f"{source_path} missing *_KEY suffix rule"
