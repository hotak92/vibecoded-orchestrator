"""vco_lib.containers — canonical container-name registry.

Single source of truth for the names VCO uses for its infrastructure
containers (Weaviate, Ollama, the code-embedding service). Centralised
here so that install.py, MCP servers, hooks, tests, and any future
launcher Python code can ask one module "what container do I look for?"
and stay in lockstep with the actual `container_name:` fields shipped
in infrastructure/docker-compose.yml.

Background — the "maintainer-machine leak" (v0.2.15 fix, 2026-05-17)
-------------------------------------------------------------------
Pre-v0.2.15, install.py + MCP servers + several hooks hardcoded
``weaviate_claude`` / ``ollama_claude`` / ``code_embed_claude`` as the
fallback container name to look for or restart. Those names only ever
existed on the maintainer's own pre-VCO machine (from a
``weaviate_<project>`` per-workspace era that never shipped). VCO has
ONLY ever shipped these names publicly:

  * v0.1.x : ``weaviate`` / ``ollama`` / ``code_embed`` (unprefixed)
  * v0.2.x : ``vco_weaviate`` / ``vco_ollama`` /
             ``vco_code_embed`` (or ``vct_code_embed`` — see below)

So the historical aliases for the legacy-volume / restart-attempt
fallbacks are the UNPREFIXED names. The ``_claude``-suffixed names are
kept at the END of the alias list for the deepest possible fallback
(some maintainer-era installs may still have them on disk) but
de-emphasised; they are NOT a canonical VCO naming convention.

Renamed in v0.2.15: ``vct_code_embed`` → ``vco_code_embed``
-----------------------------------------------------------
The code-embedding container shipped as ``vct_code_embed`` in v0.2.x
for historical reasons (the launcher's Rust ``ServiceConfig::command``
and ``volume_role`` mapping pinned it). v0.2.15 renames it to
``vco_code_embed`` for naming consistency with the rest of the stack.
``vct_code_embed`` is kept in ``HISTORICAL_ALIASES`` so existing
installs migrate cleanly (the find-existing-container path still
recognises it).

Usage
-----
``canonical_name("weaviate")`` → ``"vco_weaviate"``

``all_known_names("weaviate")`` → ``["vco_weaviate", "weaviate", "weaviate_claude"]``

``find_existing_container("weaviate")`` → ``"vco_weaviate"`` if that
container exists on the user's host; falls back through the alias list
in order; returns ``None`` if none of them exist. Honours
``VCT_CONTAINER_RUNTIME=podman|docker`` per the same contract
install.py uses for runtime selection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

__all__ = [
    "CANONICAL_CONTAINERS",
    "HISTORICAL_ALIASES",
    "canonical_name",
    "all_known_names",
    "find_existing_container",
    "UnknownServiceError",
]


# ---------------------------------------------------------------------------
# Canonical names — these MUST match the `container_name:` fields shipped
# in infrastructure/docker-compose.yml and claude_mcp_servers/compose.yaml.
# A drift between this table and either compose file means the SessionStart
# hook will try to start the wrong container.
# ---------------------------------------------------------------------------
CANONICAL_CONTAINERS: dict[str, str] = {
    "weaviate":   "vco_weaviate",
    "ollama":     "vco_ollama",
    "code_embed": "vco_code_embed",
}


# ---------------------------------------------------------------------------
# Historical aliases — container names users may have on disk from older
# VCO releases (or, for the ``_claude``-suffixed names, from the
# maintainer's own pre-VCO machine). Sorted MOST RECENT FIRST so the
# find-existing-container probe prefers the freshest legacy install over
# the deepest one.
#
# Per-service ordering:
#   weaviate:   canonical | v0.1.x unprefixed | maintainer-era
#   ollama:     canonical | v0.1.x unprefixed | maintainer-era
#   code_embed: canonical | v0.2.x vct-prefix | v0.1.x unprefixed | maintainer-era
#
# The canonical name IS NOT duplicated here — `all_known_names()` prepends
# it. This keeps the alias list a pure "things that ARE NOT the canonical
# name but might exist" registry.
# ---------------------------------------------------------------------------
HISTORICAL_ALIASES: dict[str, list[str]] = {
    "weaviate":   ["weaviate", "weaviate_claude"],
    "ollama":     ["ollama",   "ollama_claude"],
    "code_embed": ["vct_code_embed", "code_embed", "code_embed_claude"],
}


class UnknownServiceError(KeyError):
    """Raised when a caller asks for a service name not in the registry."""


def _validate_service(service: str) -> None:
    if service not in CANONICAL_CONTAINERS:
        known = ", ".join(sorted(CANONICAL_CONTAINERS))
        raise UnknownServiceError(
            f"Unknown VCO service {service!r}. Known: {known}."
        )


def canonical_name(service: str) -> str:
    """Return the canonical container name for ``service``.

    >>> canonical_name("weaviate")
    'vco_weaviate'
    >>> canonical_name("code_embed")
    'vco_code_embed'

    Raises ``UnknownServiceError`` if ``service`` is not in
    ``CANONICAL_CONTAINERS``.
    """
    _validate_service(service)
    return CANONICAL_CONTAINERS[service]


def all_known_names(service: str) -> list[str]:
    """Return every name a probe should check for ``service``.

    Order: canonical first, then ``HISTORICAL_ALIASES[service]`` in
    declaration order (most recent legacy → oldest legacy).

    Duplicates are filtered while preserving order, so an alias that
    happens to equal the canonical name (defensive — none currently do)
    only appears once.

    >>> all_known_names("weaviate")
    ['vco_weaviate', 'weaviate', 'weaviate_claude']
    """
    _validate_service(service)
    seen: set[str] = set()
    out: list[str] = []
    for name in (canonical_name(service), *HISTORICAL_ALIASES[service]):
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _resolve_runtime(runtime: str) -> Optional[str]:
    """Resolve the runtime hint to an executable on PATH.

    The ``VCT_CONTAINER_RUNTIME`` env var overrides the caller-passed
    default (matching install.py's contract). Recognised values are
    ``podman``, ``docker``, and ``auto`` (or unset). ``auto`` triggers
    podman-first probing. Unknown values are ignored and we fall through
    to the caller-passed default.

    Returns the actual executable name (``podman`` or ``docker``) that
    is present on PATH, or ``None`` if neither is available.
    """
    env_raw = os.environ.get("VCT_CONTAINER_RUNTIME", "").strip().lower()
    if env_raw in ("podman", "docker"):
        effective = env_raw
    elif env_raw == "auto" or not env_raw:
        effective = (runtime or "podman").strip().lower()
    else:
        # Unrecognised env value — don't error here (this module is
        # called from very hot paths). Fall through to the default.
        effective = (runtime or "podman").strip().lower()

    if effective not in ("podman", "docker"):
        # Caller passed something weird. Probe both in podman-first order.
        for candidate in ("podman", "docker"):
            if shutil.which(candidate):
                return candidate
        return None

    if shutil.which(effective):
        return effective

    # Effective choice missing — probe the other.
    other = "docker" if effective == "podman" else "podman"
    if shutil.which(other):
        return other
    return None


def find_existing_container(
    service: str, runtime: str = "podman",
) -> Optional[str]:
    """Return the first container name from ``all_known_names(service)``
    that actually exists on the user's host, or ``None`` if none do.

    Uses ``<runtime> container exists <name>`` which returns exit 0 when
    the container exists (running OR stopped) and non-zero otherwise.
    Read-only probe; never mutates state.

    Runtime selection follows the same contract as install.py:
      * ``VCT_CONTAINER_RUNTIME`` env var (if set to ``podman`` or
        ``docker``) wins over the ``runtime`` argument.
      * ``runtime="podman"`` (default) is used when the env is unset or
        set to ``auto``.
      * If the chosen runtime isn't on PATH, the function probes the
        OTHER runtime as a fallback before giving up.

    Returns ``None`` when:
      * Neither podman nor docker is on PATH.
      * The runtime is present but ``<runtime> container exists`` fails
        for every alias.
      * ``service`` is not in the canonical registry — but in that case
        we raise ``UnknownServiceError`` instead of silently returning
        None, because a typo in the service name is a programming error,
        not a runtime condition.
    """
    # Validate first; bad service names are programmer errors.
    _validate_service(service)

    bin_name = _resolve_runtime(runtime)
    if bin_name is None:
        return None

    for name in all_known_names(service):
        try:
            res = subprocess.run(
                [bin_name, "container", "exists", name],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            # Don't let a single hung/missing probe poison the whole
            # search — try the next alias.
            continue
        if res.returncode == 0:
            return name
    return None
