# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Host capability probes — system RAM, physical CPU cores, usable GPU VRAM.

ONE home for the three numbers every hardware-tier selector in
:mod:`vco_lib.embedding_selection` takes. Extracted from ``install.py``
(v0.2.102 lane W5-D) when a SECOND caller appeared: the runtime summary-model
resolver (:mod:`vco_lib.summary_model`) falls back to the same tier probe
install uses when no install record exists, and a second copy of the probes
would let the two tier readings drift (the v0.2.23 C10 lesson).

Stdlib only at module scope (install.py imports this before its venv
exists): ``psutil`` is imported lazily and only REFINES an answer the stdlib
fallbacks already give. Every probe soft-fails to 0 — the selectors read 0
as the low-end tier, the conservative direction.
"""
from __future__ import annotations

import functools
import os
import platform
import subprocess
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # stdlib-only at RUNTIME scope — gpu_device imports lazily
    from vco_lib.gpu_device import GpuCandidate


def probe_system_ram_gb() -> float:
    """Best-effort system RAM probe across Linux/macOS/Windows.

    Uses `psutil` if available (fastest and most portable). Falls back
    to /proc/meminfo on Linux, `sysctl hw.memsize` on macOS, and `wmic`
    on Windows. Returns 0.0 on any failure — caller handles gracefully.
    """
    try:
        import psutil  # type: ignore
        return round(psutil.virtual_memory().total / (1024.0 ** 3), 2)
    except ImportError:
        pass
    except Exception:
        return 0.0

    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024.0 ** 2), 2)
        elif system == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return round(int(result.stdout.strip()) / (1024.0 ** 3), 2)
        elif system == "Windows":
            result = subprocess.run(
                ["wmic", "ComputerSystem", "get", "TotalPhysicalMemory"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    s = line.strip()
                    if s.isdigit():
                        return round(int(s) / (1024.0 ** 3), 2)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return 0.0


def probe_cpu_cores() -> int:
    """Best-effort PHYSICAL CPU-core count, cross-OS.

    v0.2.49: switched from logical=True (counts SMT threads) to
    logical=False (counts physical cores) after post-update validation on a
    contributor's Windows box showed the embedding-model auto-selector tier-up'd to
    qwen3 on a host where the logical count (16) crossed the >=8
    threshold even though the actual physical cores (8) were exactly at
    the boundary — qwen3-embedding on CPU-only Ollama takes ~30s per
    embedding, so the selector should require margin, not just count
    HyperThreads. Embedding throughput on CPU is gated by physical
    arithmetic units, not thread count.

    Prefers `psutil.cpu_count(logical=False)` (returns the PHYSICAL
    core count; cross-OS, handles cgroup limits on Linux containers,
    docs explicitly: "the number of physical cores only"). Falls back to
    `os.cpu_count() // 2` (a conservative estimate assuming SMT, which
    is true on most modern Intel/AMD desktops + Apple Silicon's
    performance/efficiency split where logical count overstates
    embedding-capable cores). Returns 0 on any probe failure — the
    selectors treat 0 as "low-end CPU" (drops to the smaller-model
    tier), which is the conservative direction (better to under-spec
    than over-promise).
    """
    try:
        import psutil  # type: ignore
        n = psutil.cpu_count(logical=False)
        if n and n > 0:
            return int(n)
        # psutil returns None on some hypervisors / containers when
        # physical-core probing isn't supported. Fall through to
        # os.cpu_count() // 2 as a conservative estimate.
    except ImportError:
        pass
    except Exception:
        return 0
    try:
        n = os.cpu_count()
        if n and n > 0:
            # Conservative: assume SMT/HT, halve the logical count.
            # On non-SMT hardware this under-counts by 2x, which is the
            # safer direction (we drop to a smaller model tier).
            return max(1, int(n) // 2)
        return 0
    except Exception:
        return 0


def probe_gpu_vram_gb() -> float:
    """VRAM (GB) of the most capable USABLE discrete GPU, 0.0 when none —
    the same device choice install makes (:mod:`vco_lib.gpu_device`:
    enumerate, drop Intel and integrated GPUs, pick the largest card).
    Never raises."""
    try:
        from vco_lib.gpu_device import enumerate_gpus, select_gpu_device

        chosen = select_gpu_device(enumerate_gpus())
    except Exception:
        return 0.0
    vram = getattr(chosen, "vram_gb", None) if chosen is not None else None
    return float(vram) if vram and vram > 0 else 0.0


def _select_gpu(candidates: "list[GpuCandidate]") -> "Optional[GpuCandidate]":
    """The ONE selection rule over a GIVEN enumeration: apply
    :func:`vco_lib.gpu_device.select_gpu_device` with the ``VCT_GPU_VENDOR``
    preference — exactly what install's system-info step asks for. Never
    raises; ``None`` is the CPU path."""
    from vco_lib.gpu_device import select_gpu_device
    from vco_lib.gpu_profile import gpu_vendor_preference_from_env

    try:
        return select_gpu_device(
            candidates, vendor_pref=gpu_vendor_preference_from_env())
    except Exception:  # noqa: BLE001 — a failed probe is the CPU path
        return None


@functools.lru_cache(maxsize=1)
def _selected_gpu_cached() -> "Optional[GpuCandidate]":
    """The chosen card of THIS host's own enumeration, memoised per process.

    The cache (v0.2.103 R43 N8, deliberately kept) holds the answer —
    INCLUDING the ``(None`` → CPU-path ``)`` failure answer — for the
    process lifetime: no caller needs fresh data in-process (a GPU driver
    install happens in the launcher, a separate process, and nothing
    mutates ``VCT_GPU_VENDOR`` mid-process), while the enumeration spawns
    GPU subprocesses (nvidia-smi / rocm-smi / lspci) and the resolver's
    hardware tail runs on every no-record resolution. Tests that vary the
    hardware call :meth:`_selected_gpu_cached.cache_clear()
    <functools.lru_cache.cache_clear>`."""
    try:
        from vco_lib.gpu_device import enumerate_gpus

        return _select_gpu(enumerate_gpus())
    except Exception:  # noqa: BLE001 — a failed probe is the CPU path
        return None


def selected_gpu(
    candidates: "Optional[list[GpuCandidate]]" = None,
) -> "Optional[GpuCandidate]":
    """The ONE CHOSEN CARD every hardware-aware GPU decision selects —
    v0.2.103 R43 N4, the single home of that choice: a
    :class:`vco_lib.gpu_device.GpuCandidate` (name, vendor, VRAM on one
    object), or ``None`` for the CPU path. R41 M2 made install and the
    runtime code-model resolver read the same card; this returns the card
    ITSELF so install's system-info step can pass its OWN enumeration
    (``selected_gpu(candidates)`` — no second probe round) and read name,
    vendor and VRAM off the one object the resolver would also choose.

    ``candidates is None`` (the resolver's form) enumerates and reads the
    per-process cache (:func:`_selected_gpu_cached`); an explicit
    ``candidates`` list is selected without touching the cache. Never
    raises."""
    if candidates is None:
        return _selected_gpu_cached()
    return _select_gpu(list(candidates))


def selected_gpu_specs() -> "tuple[float, str]":
    """``(vram_gb, vendor)`` of the ONE card every hardware-aware code-model
    decision selects — v0.2.103 R41 M2, the single home of that choice, as a
    thin wrapper over :func:`selected_gpu` (R43 N4: same selection, one
    object, two shapes).

    Install's own GPU detection (``install.py``'s system-info step) reads
    name, vendor and VRAM off the SAME chosen card; the runtime code-model
    resolver (:func:`vco_lib.code_embed_model._hardware_code_model`) used to
    take VRAM from one card (``probe_gpu_vram_gb``, no vendor preference) and
    vendor from another (the FIRST attributed entry of the enumeration), so
    on a 6 GB NVIDIA + 16 GB AMD host the resolver answered CodeSage while
    install answered qwen3. This helper makes the selection once, honours
    ``VCT_GPU_VENDOR`` exactly like install, and returns both numbers from
    that one card. Memoised per process (see :func:`_selected_gpu_cached`
    for why, and how tests that vary the hardware clear it).

    ``(0.0, "")`` when no usable discrete card is found or the probes fail —
    the selectors read that as the CPU path, the conservative direction.
    Never raises."""
    chosen = selected_gpu()
    if chosen is None:
        return (0.0, "")
    vram = getattr(chosen, "vram_gb", None)
    vendor = (getattr(chosen, "vendor", "") or "").strip().lower()
    return (float(vram) if vram and vram > 0 else 0.0, vendor)


__all__ = [
    "probe_cpu_cores", "probe_gpu_vram_gb", "probe_system_ram_gb",
    "selected_gpu", "selected_gpu_specs",
]
