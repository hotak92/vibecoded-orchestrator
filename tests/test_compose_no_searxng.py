# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.11 compose.yaml: SearXNG service removed.

SearXNG was added as a default service in 503b736 to back the Search
MCP's `web_search` tool. In v0.2.11 the entire `web_search` tool was
dropped (Claude itself ships WebSearch), so SearXNG is no longer
required.

These tests pin two contracts:
  1. The SearXNG service block must be gone from the default compose.
  2. The remaining services must KEEP their named-volume `name:`
     declarations (the Bug-31 fix from the same commit that added
     SearXNG). It would be easy to accidentally revert those when
     deleting the SearXNG block; this guard catches that.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "claude_mcp_servers" / "compose.yaml"


def _load_compose() -> dict:
    assert COMPOSE_FILE.is_file(), f"compose file missing: {COMPOSE_FILE}"
    with COMPOSE_FILE.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), f"compose root is not a mapping: {type(data)}"
    return data


def test_searxng_service_is_absent_from_default_compose():
    """The default compose ships no SearXNG service."""
    data = _load_compose()
    services = data.get("services") or {}
    assert "searxng" not in services, (
        f"searxng service still present in default compose: "
        f"services={sorted(services.keys())}"
    )


def test_searxng_named_volume_is_absent():
    """No SearXNG-related named volume should remain."""
    data = _load_compose()
    volumes = data.get("volumes") or {}
    forbidden = {"searxng_settings", "searxng_data", "vco_searxng_settings"}
    leaked = forbidden.intersection(volumes.keys())
    assert not leaked, (
        f"SearXNG-related named volume(s) still declared: {sorted(leaked)}"
    )


def test_remaining_services_have_named_volume_declarations():
    """Regression guard against accidentally undoing the Bug-31 fix.

    Bug-31 required explicit `name:` declarations on every named
    volume so docker-compose.override.yml could alias them to external
    pre-existing volumes (without that, podman-compose 1.5.x crashes
    with `can't merge value of [<vol>] of type NoneType and dict`).
    These names MUST stay even though the SearXNG service is gone.
    """
    data = _load_compose()
    volumes = data.get("volumes") or {}

    required = {
        "weaviate_data":    "vco_weaviate_data",
        "ollama_models":    "vco_ollama_models",
        "code_embed_cache": "vco_code_embed_cache",
    }
    for vol_key, expected_name in required.items():
        assert vol_key in volumes, (
            f"required named volume '{vol_key}' missing from compose"
        )
        entry = volumes[vol_key]
        assert isinstance(entry, dict), (
            f"volume '{vol_key}' must be a mapping with 'name:' (Bug-31 fix), "
            f"got {entry!r}"
        )
        actual_name = entry.get("name")
        assert actual_name == expected_name, (
            f"volume '{vol_key}' has wrong 'name:' value "
            f"(expected {expected_name!r}, got {actual_name!r}) — "
            "Bug-31 fix may have been reverted"
        )


def test_weaviate_and_ollama_services_still_present():
    """The compose simplification dropped SearXNG only; core services stay."""
    data = _load_compose()
    services = data.get("services") or {}
    for required_service in ("weaviate", "ollama"):
        assert required_service in services, (
            f"core service '{required_service}' missing — only SearXNG "
            f"was supposed to be removed. services={sorted(services.keys())}"
        )


def test_compose_yaml_is_valid_yaml_and_has_project_name():
    """Belt-and-suspenders: the file parses, has a project name, and a
    network. (If the file was malformed by a careless edit, every other
    test in this module would also fail — this one gives a clearer
    error message in that case.)"""
    data = _load_compose()
    assert data.get("name") == "vibecoded", (
        f"compose project name changed (expected 'vibecoded'): {data.get('name')!r}"
    )
    networks = data.get("networks") or {}
    assert "vibecoded-network" in networks, (
        f"vibecoded-network missing from compose: {sorted(networks.keys())}"
    )


if __name__ == "__main__":
    # Allow `python tests/test_compose_no_searxng.py` for manual smoke check.
    for name, obj in list(globals().items()):
        if name.startswith("test_") and callable(obj):
            try:
                obj()
                print(f"OK   {name}")
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}", file=sys.stderr)
                raise
