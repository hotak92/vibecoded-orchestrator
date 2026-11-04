# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Weaviate's container memory limit, sized from the host (v0.2.105).

ONE home for the formula. install.py projects the result into
``infrastructure/.env`` as :data:`ENV_KEY` (through
:func:`vco_lib.compose_env.compose_substitution_env`, the file compose reads),
and ``infrastructure/docker-compose.yml`` sets ``mem_limit:
${VCT_WEAVIATE_MEM_LIMIT:-0}`` from it.

Why a limit at all: without one, cgroup v2 reports ``memory.max = max`` and
Weaviate's ``LIMIT_RESOURCES`` turns that into an effectively infinite Go soft
limit, so its memory guards never fire and the heap grows until the host runs
short (a 61 GB host lost its desktop session to systemd-oomd while Weaviate
held 30 GB). With a limit, Go's soft limit is 80% of it, Weaviate refuses a
write batch with "not enough memory" before reaching the cap, and on a real
OOM kill ``restart: unless-stopped`` brings it back (both verified on a
disposable container before shipping).

The formula — 40% of host RAM, floor 2 GiB, ceiling 32 GiB, rounded down to
256 MiB — leaves the rest for what shares the machine: Ollama (embedding and
summary models; on a CPU-only host those sit in RAM), the code-embedding
service, and the desktop, IDE and browser. The floor is the smallest size
that starts Weaviate with a typical project's collections; the ceiling is
more than VCO's compressed (RQ-8) collections need on any machine seen so far
(a 900k-object install's live heap was ~6-8 GB uncompressed). Examples:
8 GB host -> 3 GiB, 16 GB -> 6.25 GiB, 32 GB -> 12.75 GiB, 64 GB -> 25.5 GiB,
128 GB -> 32 GiB.

On macOS/Windows the containers run inside a VM whose own memory VCO cannot
read; the limit is then sized from the host and can exceed the VM, in which
case the VM's size is the effective bound (as before this change).
"""
from __future__ import annotations

from typing import Mapping, Optional

#: The ``infrastructure/.env`` key compose substitutes into ``mem_limit``.
ENV_KEY = "VCT_WEAVIATE_MEM_LIMIT"

FRACTION_OF_HOST = 0.40
FLOOR_MIB = 2048
CEILING_MIB = 32768
STEP_MIB = 256


def mem_limit_mib(host_ram_gb: Optional[float]) -> Optional[int]:
    """The limit in MiB for a host with *host_ram_gb* GiB of RAM, or ``None``
    when the RAM is unknown (``None`` / ``0`` / negative: the probe failed —
    write nothing rather than guess)."""
    try:
        ram = float(host_ram_gb or 0.0)
    except (TypeError, ValueError):
        return None
    if ram <= 0:
        return None
    mib = int(ram * 1024 * FRACTION_OF_HOST)
    mib = max(FLOOR_MIB, min(CEILING_MIB, mib))
    return mib - (mib % STEP_MIB)


def env_value(host_ram_gb: Optional[float],
              environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The ``VCT_WEAVIATE_MEM_LIMIT`` value install writes (``"<n>m"``), or
    ``None`` = write nothing: when the user exported the key themselves (their
    value reaches compose through its own environment, ``"0"`` = no limit), or
    when the host RAM is unknown (an existing line in ``.env`` is then kept)."""
    import os  # noqa: PLC0415

    env = os.environ if environ is None else environ
    if ENV_KEY in env:
        return None
    mib = mem_limit_mib(host_ram_gb)
    return None if mib is None else f"{mib}m"
