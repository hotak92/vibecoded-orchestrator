# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A bundled manifest's MCP claims name an MCP that something serves.

``vct-codegraph.json`` declared an ``mcp_registration`` for an MCP named
``codegraph`` that no module and no bundled server provides — the code graph
tools (``search_code_graph``, ``query_code_structure``) are tools of the
``weaviate-kg`` MCP, which vct-kg serves and registers. It could not simply be
re-pointed at ``weaviate-kg``: the uninstall path deregisters the MCP an
``mcp_registration`` names, so uninstalling vct-codegraph would have removed
vct-kg's MCP.

The rule, over every bundled manifest:

* an ``mcp_registration`` belongs to a module that SERVES an MCP (its runtime
  is ``mcp_*``), and names one of the MCPs VCO registers
  (``mcp_scan_rules.toml`` ``[entries].default_names``);
* ``uninstall.deregister_mcp`` is only true where there is an MCP to
  deregister;
* a ``provides`` entry of kind ``mcp_tools`` names, as ``tool_prefix``, an MCP
  some bundled manifest registers.

The Rust twin over the EMBEDDED list is
``bundled_manifests::tests::every_mcp_claim_names_an_mcp_a_bundled_module_serves``.
"""
from __future__ import annotations

import json
from pathlib import Path

from vco_lib import mcp_scan_rules

REPO = Path(__file__).resolve().parents[1]
MANIFEST_DIR = REPO / "launcher" / "bundled_manifests"


def _manifests() -> dict[str, dict]:
    return {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(MANIFEST_DIR.glob("*.json"))
    }


def mcp_claim_problems(manifests: dict[str, dict], registered_names: set[str]) -> list[str]:
    """Every false MCP claim in ``manifests``, as ``"<file>: <why>"``."""
    # An MCP counts as served only by a module that runs it and that VCO
    # registers — a phantom registration cannot vouch for its own tools.
    served = {
        m["mcp_registration"]["mcp_name"]
        for m in manifests.values()
        if m.get("mcp_registration")
        and str(m.get("runtime", {}).get("type", "")).startswith("mcp")
        and m["mcp_registration"]["mcp_name"] in registered_names
    }
    problems: list[str] = []
    for name, m in manifests.items():
        reg = m.get("mcp_registration")
        runtime_type = str(m.get("runtime", {}).get("type", ""))
        if reg:
            if not runtime_type.startswith("mcp"):
                problems.append(
                    f"{name}: registers MCP {reg['mcp_name']!r} but its runtime is "
                    f"{runtime_type!r}, not an MCP server"
                )
            if reg["mcp_name"] not in registered_names:
                problems.append(
                    f"{name}: registers MCP {reg['mcp_name']!r}, which VCO never registers"
                )
        if m.get("uninstall", {}).get("deregister_mcp") and not reg:
            problems.append(f"{name}: uninstall.deregister_mcp is true with no MCP of its own")
        for entry in m.get("provides", []):
            if entry.get("kind") == "mcp_tools" and entry.get("tool_prefix") not in served:
                problems.append(
                    f"{name}: provides mcp_tools under {entry.get('tool_prefix')!r}, "
                    "an MCP no bundled manifest registers"
                )
    return problems


def test_every_bundled_mcp_claim_is_true() -> None:
    registered = set(mcp_scan_rules.default_mcp_entry_names())
    assert mcp_claim_problems(_manifests(), registered) == []


def test_the_code_graph_tools_are_claimed_under_the_mcp_that_serves_them() -> None:
    manifests = _manifests()
    codegraph = manifests["vct-codegraph.json"]
    assert "mcp_registration" not in codegraph
    assert codegraph["uninstall"]["deregister_mcp"] is False
    kg = manifests["vct-kg.json"]
    assert kg["mcp_registration"]["mcp_name"] == "weaviate-kg"
    assert "vct-kg" in codegraph["requirements"]["depends_on"]
    prefixes = {e["tool_prefix"] for e in codegraph["provides"] if e["kind"] == "mcp_tools"}
    assert prefixes == {"weaviate-kg"}


def test_the_rule_catches_each_false_claim_it_names() -> None:
    """The check's own red proof, on synthetic manifests."""
    registered = {"weaviate-kg"}
    kg = {
        "runtime": {"type": "mcp_stdio"},
        "mcp_registration": {"mcp_name": "weaviate-kg"},
        "uninstall": {"deregister_mcp": True},
        "provides": [{"kind": "mcp_tools", "tool_prefix": "weaviate-kg"}],
    }
    assert mcp_claim_problems({"kg.json": kg}, registered) == []

    phantom = {
        "runtime": {"type": "cli"},
        "mcp_registration": {"mcp_name": "codegraph"},
        "uninstall": {"deregister_mcp": True},
        "provides": [{"kind": "mcp_tools", "tool_prefix": "codegraph"}],
    }
    found = mcp_claim_problems({"kg.json": kg, "old-codegraph.json": phantom}, registered)
    assert any("runtime is 'cli'" in p for p in found), found
    assert any("which VCO never registers" in p for p in found), found
    assert any("under 'codegraph'" in p for p in found), found

    orphan_deregister = {"runtime": {"type": "cli"}, "uninstall": {"deregister_mcp": True}}
    assert mcp_claim_problems({"x.json": orphan_deregister}, registered) == [
        "x.json: uninstall.deregister_mcp is true with no MCP of its own"
    ]
