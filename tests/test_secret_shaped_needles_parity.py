# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""B-3 (v0.2.73) — cross-language parity for the secret-shaped-key denylist.

The "is this env key credential-shaped?" needle set is duplicated across FOUR
implementations that MUST agree, or a credential-named key could leak into
``~/.claude.json`` from one registration path while another drops it (the exact
leak the denylist exists to prevent — see B-3 finding):

  * ``install.py::_SECRET_SHAPED_SUBSTRINGS``            (Python, canonical-ish)
  * ``vco_lib/secrets_audit.py::_SECRET_SHAPED_SUBSTRINGS`` (Python mirror)
  * ``launcher/src-tauri/src/mcp_registration.rs``       (Rust ``needles`` array)

Each side already has its OWN unit test, but NONE asserts the Python list ==
the Rust list. This module is that single durable guard. It does NOT edit the
needle lists — it parses all three source-of-truth definitions and asserts they
are byte-identical sets, and that the `KEY`/`*_KEY` extra rule is present in
every implementation. Full de-dup to one shared source is impractical across
Rust/Python; a parity test is the right "mirror-don't-fork" enforcement.

A future PR that adds e.g. ``CREDENTIAL`` to only one implementation trips this
test loudly at CI time rather than shipping a silent divergence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

_INSTALL_PY = _REPO / "install.py"
_SECRETS_AUDIT_PY = _REPO / "vco_lib" / "secrets_audit.py"
_MCP_REGISTRATION_RS = (
    _REPO / "launcher" / "src-tauri" / "src" / "mcp_registration.rs"
)


def _parse_python_substrings(source_path: Path) -> set[str]:
    """Extract the string literals from the ``_SECRET_SHAPED_SUBSTRINGS`` tuple
    in a Python source file (works for the plain tuple and the ``: Tuple[...]``
    annotated form)."""
    text = source_path.read_text(encoding="utf-8")
    # Match `_SECRET_SHAPED_SUBSTRINGS ... = ( ... )` up to the closing paren.
    m = re.search(
        r"_SECRET_SHAPED_SUBSTRINGS[^\n=]*=\s*\((?P<body>.*?)\)",
        text,
        re.DOTALL,
    )
    assert m is not None, f"could not find _SECRET_SHAPED_SUBSTRINGS in {source_path}"
    literals = re.findall(r'"([^"]+)"', m.group("body"))
    return set(literals)


def _parse_rust_needles(source_path: Path) -> set[str]:
    """Extract the string literals from the Rust ``let needles = [ ... ];``
    array inside ``is_secret_shaped_env_key``."""
    text = source_path.read_text(encoding="utf-8")
    m = re.search(r"let\s+needles\s*=\s*\[(?P<body>.*?)\]", text, re.DOTALL)
    assert m is not None, f"could not find `needles` array in {source_path}"
    literals = re.findall(r'"([^"]+)"', m.group("body"))
    return set(literals)


#: The canonical needle set (asserted identical across every implementation).
_EXPECTED_NEEDLES = {"TOKEN", "SECRET", "PAT", "PASSWORD", "PASS", "AUTH"}


def test_install_py_needles_match_expected():
    assert _parse_python_substrings(_INSTALL_PY) == _EXPECTED_NEEDLES


def test_secrets_audit_needles_match_install_py():
    install = _parse_python_substrings(_INSTALL_PY)
    audit = _parse_python_substrings(_SECRETS_AUDIT_PY)
    assert audit == install, (
        "vco_lib/secrets_audit.py::_SECRET_SHAPED_SUBSTRINGS drifted from "
        f"install.py: install={sorted(install)} audit={sorted(audit)}"
    )


def test_rust_needles_match_python():
    """The Rust mcp_registration.rs needle array MUST equal the Python set."""
    py = _parse_python_substrings(_INSTALL_PY)
    rust = _parse_rust_needles(_MCP_REGISTRATION_RS)
    assert rust == py, (
        "launcher/src-tauri/src/mcp_registration.rs `needles` drifted from "
        f"install.py::_SECRET_SHAPED_SUBSTRINGS: python={sorted(py)} "
        f"rust={sorted(rust)}. Update BOTH (mirror-don't-fork)."
    )


def test_all_three_agree_transitively():
    """One assertion that pins all three sources to the same set."""
    install = _parse_python_substrings(_INSTALL_PY)
    audit = _parse_python_substrings(_SECRETS_AUDIT_PY)
    rust = _parse_rust_needles(_MCP_REGISTRATION_RS)
    assert install == audit == rust == _EXPECTED_NEEDLES


@pytest.mark.parametrize(
    "source_path",
    [_INSTALL_PY, _SECRETS_AUDIT_PY, _MCP_REGISTRATION_RS],
)
def test_key_extra_rule_present_in_every_impl(source_path):
    """Every implementation adds the same extra `KEY` / `*_KEY` rule beyond the
    needle segments. Assert the rule text is present so a copy that drops it
    (and would then MISS `OPENAI_API_KEY` etc.) trips this test."""
    text = source_path.read_text(encoding="utf-8")
    # Python: `upper == "KEY" or upper.endswith("_KEY")`
    # Rust:   `upper == "KEY" || upper.ends_with("_KEY")`
    assert '"KEY"' in text, f"{source_path} missing exact-KEY rule"
    assert "_KEY" in text, f"{source_path} missing *_KEY suffix rule"
