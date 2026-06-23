# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the v0.2.64 code_embed -> Ollama in-network DNS fix.

The bug: when CODE_EMBED_BACKEND=ollama (the CPU fallback path), the
code_embed container defaulted to OLLAMA_URL=http://localhost:11435. Inside
the container `localhost` is the code_embed container ITSELF, not the Ollama
container, so the ollama backend's /health probe failed with
"Connection refused localhost:11435/api/embeddings".

The fix injects OLLAMA_URL into the code_embed service environment pointing at
the in-network DNS name `http://vco_ollama:11434` (11434 = Ollama's
IN-CONTAINER port, not the host-mapped 11435), overridable via
CODE_EMBED_OLLAMA_URL, and orders code_embed after ollama via depends_on.

These tests pin three contracts:
  1. The compose injects OLLAMA_URL for code_embed at the correct in-network
     target, behind a CODE_EMBED_OLLAMA_URL override, leaving the default
     gpu/codesage backend untouched.
  2. code_embed starts after ollama (depends_on) so the DNS name resolves.
  3. The service source actually CONSUMES the var we inject (OLLAMA_URL, not
     OLLAMA_HOST) — so the compose contract stays bound to the code.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "infrastructure" / "docker-compose.yml"
SERVICE_SRC = (
    REPO_ROOT
    / "claude_mcp_servers"
    / "code_embedding_service"
    / "server.py"
)

# The in-network address: Ollama's container_name + its IN-CONTAINER port.
EXPECTED_OLLAMA_URL = "http://vco_ollama:11434"


def _load_compose() -> dict:
    assert COMPOSE_FILE.is_file(), f"compose file missing: {COMPOSE_FILE}"
    with COMPOSE_FILE.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), f"compose root is not a mapping: {type(data)}"
    return data


def _code_embed_service() -> dict:
    data = _load_compose()
    services = data.get("services") or {}
    assert "code_embed" in services, (
        f"code_embed service missing from compose: services={sorted(services)}"
    )
    svc = services["code_embed"]
    assert isinstance(svc, dict), f"code_embed is not a mapping: {svc!r}"
    return svc


def test_code_embed_injects_ollama_url_at_in_network_dns():
    """code_embed must reach Ollama via the in-network DNS name on 11434.

    The default points at vco_ollama:11434 (NOT localhost:11435) so the
    ollama backend works inside the container, with a CODE_EMBED_OLLAMA_URL
    override for power users (e.g. host-side Ollama).
    """
    env = _code_embed_service().get("environment") or {}
    assert isinstance(env, dict), (
        f"code_embed environment must be a mapping (dict form): {env!r}"
    )
    assert "OLLAMA_URL" in env, (
        "code_embed must inject OLLAMA_URL so the ollama backend reaches "
        f"Ollama via in-network DNS; env keys={sorted(env)}"
    )
    value = env["OLLAMA_URL"]
    assert value == f"${{CODE_EMBED_OLLAMA_URL:-{EXPECTED_OLLAMA_URL}}}", (
        "OLLAMA_URL must default to the in-network DNS target behind a "
        f"CODE_EMBED_OLLAMA_URL override, got {value!r}"
    )
    # The literal must NOT be the host-loopback default (the bug).
    assert "localhost:11435" not in value, (
        "OLLAMA_URL still points at localhost:11435 — inside the container "
        "that is code_embed itself, not Ollama"
    )
    # The in-container port (11434) must be targeted, not the host map (11435).
    assert ":11434" in value and ":11435" not in value, (
        "OLLAMA_URL must target Ollama's IN-CONTAINER port 11434, not the "
        f"host-mapped 11435: {value!r}"
    )


def test_gpu_default_backend_unchanged():
    """The fix must not change the default gpu/codesage backend."""
    env = _code_embed_service().get("environment") or {}
    assert env.get("CODE_EMBED_BACKEND") == "${CODE_EMBED_BACKEND:-gpu}", (
        f"default backend changed: {env.get('CODE_EMBED_BACKEND')!r}"
    )
    assert env.get("CODE_EMBED_MODEL") == "codesage/codesage-large-v2", (
        f"default model changed: {env.get('CODE_EMBED_MODEL')!r}"
    )


def test_code_embed_starts_after_ollama():
    """code_embed must depend on ollama so the DNS name resolves on probe."""
    svc = _code_embed_service()
    depends = svc.get("depends_on")
    assert depends is not None, "code_embed must declare depends_on: [ollama]"
    # Accept both the short-form list and the long-form condition mapping.
    if isinstance(depends, dict):
        deps = set(depends.keys())
    else:
        deps = set(depends)
    assert "ollama" in deps, (
        f"code_embed must depend on the ollama service, got {depends!r}"
    )


def test_ollama_service_is_named_vco_ollama_on_11434():
    """Pin the assumptions the OLLAMA_URL default relies on.

    The injected default (vco_ollama:11434) is only correct if the ollama
    service keeps container_name=vco_ollama and listens on 11434 in-container.
    Guard against a future compose edit silently breaking the DNS target.
    """
    data = _load_compose()
    ollama = (data.get("services") or {}).get("ollama") or {}
    assert ollama.get("container_name") == "vco_ollama", (
        "OLLAMA_URL default targets vco_ollama; container_name changed: "
        f"{ollama.get('container_name')!r}"
    )
    ports = ollama.get("ports") or []
    in_container_ports = {str(p).split(":")[-1] for p in ports}
    assert "11434" in in_container_ports, (
        "Ollama must listen on in-container port 11434 (the OLLAMA_URL "
        f"default target); ports={ports!r}"
    )


def test_service_source_consumes_OLLAMA_URL_not_OLLAMA_HOST():
    """Bind the compose contract to the code: server.py reads OLLAMA_URL.

    If the service ever switches to a different env var name, the compose
    injection would silently stop working — this test fails loudly instead.
    """
    src = SERVICE_SRC.read_text(encoding="utf-8")
    assert re.search(r'os\.getenv\(\s*["\']OLLAMA_URL["\']', src), (
        "code_embedding_service/server.py must read OLLAMA_URL via os.getenv; "
        "the compose injection is keyed to that exact name"
    )
    # It must NOT have silently moved to OLLAMA_HOST (which we don't inject).
    assert not re.search(r'os\.getenv\(\s*["\']OLLAMA_HOST["\']', src), (
        "service now reads OLLAMA_HOST but compose injects OLLAMA_URL — "
        "the two have drifted; update the compose env to match the code"
    )


if __name__ == "__main__":
    # Allow `python tests/test_code_embed_ollama_url.py` for a manual smoke check.
    for name, obj in list(globals().items()):
        if name.startswith("test_") and callable(obj):
            try:
                obj()
                print(f"OK   {name}")
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}", file=sys.stderr)
                raise
