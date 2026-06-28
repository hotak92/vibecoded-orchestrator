# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Multi-GPU / iGPU-aware GPU device selection (v0.2.68 Defect Y).

VCO historically did **first-vendor / first-card** GPU detection: it
probed NVIDIA, then AMD, then Metal, took the FIRST card of the FIRST
vendor that answered, and read only device-0's VRAM. That under-tiers a
multi-GPU box (24 GB + 8 GB → whatever card 0 is) and — worse — on an
AMD-iGPU + AMD-discrete host it could read the iGPU's small shared
(UMA) memory and demote a CodeSage-capable discrete card all the way to
the Jina floor (violating the "AMD-capable → qwen3, NEVER Jina"
invariant — see :mod:`vco_lib.embedding_selection`).

This module replaces that with a two-stage, *testable* pipeline:

- :func:`enumerate_gpus` — thin, probe-only. Parses ``nvidia-smi`` (ALL
  rows, with per-GPU VRAM) and ``rocm-smi`` (all rows) cross-OS, plus
  ``lspci`` (Linux vendor + PCI-bus discrimination). NVIDIA and AMD are
  the only supported accelerators, so they are the only vendors probed.
  There is intentionally NO Windows Intel-iGPU enumeration: Intel GPUs
  (iGPU or discrete) are unsupported (dropped by :func:`select_gpu_device`
  anyway), so a Windows host with only an Intel iGPU correctly enumerates
  to ``[]`` → CPU path. (``lspci`` returns ``[]`` on Windows where it is
  not present; that is the expected no-op.) Soft-fails every probe to
  ``[]`` — NEVER raises, NEVER hangs past the per-probe timeout. The
  decision lives elsewhere so this layer stays free of policy.
- :func:`select_gpu_device` — PURE. Takes a list of candidates and
  returns the chosen :class:`GpuCandidate` (or ``None`` → CPU path). It
  filters out Intel GPUs (unsupported) and integrated GPUs (never a
  usable accelerator for the VCO model stack), then picks the
  most-capable remaining discrete card by VRAM. Fully unit-testable over
  synthetic candidate lists — no subprocess mocking required.

Hardware constraints baked in (user-specified, v0.2.68):
  * NVIDIA never ships an iGPU → an iGPU is always AMD or Intel.
  * Real dual-GPU cases: {AMD or Intel iGPU} + {NVIDIA or AMD discrete}.
  * Intel GPUs (iGPU or discrete) are NOT SUPPORTED → treated as "not a
    usable accelerator" → drop → CPU path if nothing else remains.
  * Device selection = enumerate all → EXCLUDE Intel + EXCLUDE iGPUs →
    among remaining usable discrete (NVIDIA/AMD) pick MOST CAPABLE by
    VRAM → none usable → CPU path.

Conservative-default discipline (per CLAUDE.md "classify-as-keep on
uncertainty"): when iGPU-vs-discrete or vendor can't be positively
determined, the card is treated as a usable DISCRETE card rather than
silently dropped — we never strand a GPU the user knows is there.

This module is a pure leaf — it depends only on stdlib / subprocess and
must NOT import ``install`` (``install`` imports this module, not
vice-versa). It mirrors the soft-fail probe discipline of
``install._gpu_tool_reports_live`` / ``install._detect_nvidia_gpu``.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess  # noqa: S404 — probing GPU CLIs is the whole point
import sys
from dataclasses import dataclass
from typing import Optional

# Per-probe subprocess timeout (seconds). Matches the existing GPU probes
# in install.py (``_detect_nvidia_gpu`` / ``_probe_*_vram_gb`` all use 10).
# Kept here as a constant so callers / tests can reason about it; we never
# add a *global* detection timeout (that would strand detection on a slow
# machine — see the "no global install timeout" feedback note).
_PROBE_TIMEOUT_S = 10

# PCI vendor IDs as they appear in `lspci -nn` bracketed [VEN:DEV] tokens
# (e.g. "[8086:...]"). Used only by the Linux lspci classifier below; there
# is no Windows PNPDeviceID path (see module docstring — Intel is unsupported
# and NVIDIA/AMD are probed cross-OS via nvidia-smi/rocm-smi).
_VENDOR_ID_INTEL = "8086"
_VENDOR_ID_NVIDIA = "10de"
_VENDOR_ID_AMD = "1002"

# An AMD APU's integrated Radeon carves "VRAM" from system RAM (UMA),
# typically 512 MB - 2 GB. A genuine discrete card sits well above this.
# Used ONLY as a backstop when PCI-bus classification is unavailable —
# never as the sole signal (a low-VRAM *discrete* card would be a false
# positive, so we require the low-VRAM signal to co-occur with an absent
# bus address before flagging integrated).
_IGPU_UMA_VRAM_CEILING_GB = 2.0


@dataclass(frozen=True)
class GpuCandidate:
    """One enumerated GPU.

    Attributes:
        vendor: ``"nvidia"`` | ``"amd"`` | ``"intel"`` | ``"unknown"``.
            ``"unknown"`` is the conservative classification when a probe
            saw a card but couldn't attribute a vendor — it is treated as
            usable-discrete by :func:`select_gpu_device` (keep-on-uncertainty).
        name: Human-readable product string (may be empty).
        vram_gb: Total VRAM in GB. ``0.0`` means "unknown / probe failed"
            — NOT "no memory".
        is_integrated: ``True`` for an iGPU/APU. Conservatively ``False``
            when classification is uncertain.
        pci_bus: Best-effort PCI bus token (e.g. ``"03:00.0"`` / ``"00:02.0"``),
            empty when unknown. Discrete cards sit on a PCIe root port
            (``01:``/``03:``/…); integrated graphics share the CPU root
            complex (``00:``).
    """

    vendor: str
    name: str
    vram_gb: float
    is_integrated: bool
    pci_bus: str = ""


# ---------------------------------------------------------------------------
# Probe layer (thin, soft-fail, never raises)
# ---------------------------------------------------------------------------

def _run_probe(cmd: list[str], timeout: int = _PROBE_TIMEOUT_S) -> Optional[str]:
    """Run ``cmd``; return its stdout on exit-0, else ``None``.

    Soft-fails (returns ``None``) when the tool is missing, exits nonzero,
    times out, or raises any OSError. NEVER raises. Mirrors the guard
    pattern in ``install._gpu_tool_reports_live``.
    """
    if not shutil.which(cmd[0]):
        return None
    try:
        result = subprocess.run(  # noqa: S603 — args are literal tool invocations
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _enumerate_nvidia() -> list[GpuCandidate]:
    """Enumerate ALL NVIDIA GPUs via nvidia-smi (one row per device).

    Every NVIDIA GPU is discrete (HW constraint: NVIDIA makes no iGPU),
    so ``is_integrated`` is always ``False``. ``nvidia-smi`` also exposes
    a stable ``pci.bus_id`` field we capture for completeness.
    """
    out = _run_probe([
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,pci.bus_id",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return []
    cands: list[GpuCandidate] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        # Expected: index, name, memory.total(MiB), pci.bus_id
        name = parts[1] if len(parts) > 1 else ""
        vram_gb = 0.0
        if len(parts) > 2:
            try:
                vram_gb = round(float(parts[2]) / 1024.0, 2)
            except ValueError:
                vram_gb = 0.0
        # nvidia-smi pci.bus_id looks like "00000000:03:00.0"; reduce to
        # the bus:slot.func tail so it matches the lspci token shape.
        pci_bus = ""
        if len(parts) > 3 and parts[3]:
            pci_bus = _normalize_pci_bus(parts[3])
        cands.append(GpuCandidate(
            vendor="nvidia", name=name, vram_gb=vram_gb,
            is_integrated=False, pci_bus=pci_bus,
        ))
    return cands


def _enumerate_amd() -> list[GpuCandidate]:
    """Enumerate AMD GPUs via rocm-smi (per-card VRAM).

    rocm-smi has no stable cross-version "device type" field, so iGPU vs
    discrete classification is left to :func:`_classify_amd_integrated`
    (cross-referenced against lspci + a UMA-VRAM backstop). Here we only
    parse per-card VRAM + an optional product name.
    """
    cands: list[GpuCandidate] = []
    # CSV form (newer rocm-smi): one data row per card.
    out = _run_probe(["rocm-smi", "--showmeminfo", "vram", "--csv"])
    if out:
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if len(lines) >= 2:
            header = [h.strip().lower() for h in lines[0].split(",")]
            vram_col = None
            for i, h in enumerate(header):
                if "vram" in h and "total" in h:
                    vram_col = i
                    break
            if vram_col is not None:
                for row in lines[1:]:
                    values = [v.strip() for v in row.split(",")]
                    if vram_col >= len(values):
                        continue
                    try:
                        bytes_val = float(values[vram_col])
                    except ValueError:
                        continue
                    vram_gb = round(bytes_val / (1024.0 ** 3), 2)
                    cands.append(GpuCandidate(
                        vendor="amd", name="AMD GPU (ROCm)",
                        vram_gb=vram_gb, is_integrated=False,
                    ))
    if cands:
        return cands
    # Text fallback (older rocm-smi): one "Total Memory" line per card.
    out = _run_probe(["rocm-smi", "--showmeminfo", "vram"])
    if out:
        for line in out.splitlines():
            ll = line.lower()
            if "total" in ll and "memory" in ll and ":" in line:
                raw = line.split(":", 1)[1].strip().split()
                if not raw:
                    continue
                try:
                    bytes_val = float(raw[0])
                except ValueError:
                    continue
                # Heuristic: huge → bytes; mid → MB; small → already GB.
                if bytes_val > 1024 ** 3:
                    vram_gb = round(bytes_val / (1024.0 ** 3), 2)
                elif bytes_val > 1024:
                    vram_gb = round(bytes_val / 1024.0, 2)
                else:
                    vram_gb = round(bytes_val, 2)
                cands.append(GpuCandidate(
                    vendor="amd", name="AMD GPU (ROCm)",
                    vram_gb=vram_gb, is_integrated=False,
                ))
    return cands


def _normalize_pci_bus(raw: str) -> str:
    """Reduce a PCI address to the ``bus:slot.func`` tail (lowercase).

    nvidia-smi reports ``00000000:03:00.0``; lspci reports ``03:00.0``.
    Normalizing both lets the AMD classifier compare bus tokens. Returns
    an empty string when no recognizable tail is found.
    """
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    # Strip a leading domain segment ("00000000:") if present.
    segs = raw.split(":")
    if len(segs) >= 3:
        # domain:bus:slot.func -> bus:slot.func
        return ":".join(segs[-2:])
    return raw


def _lspci_vga_lines() -> list[str]:
    """Return lspci ``-nn`` lines for VGA / 3D / Display controllers.

    Empty list on non-Linux, missing lspci, or any failure. Uses
    ``-nn`` so vendor IDs (``[8086:...]`` etc.) are present for robust
    vendor attribution independent of the human-readable string.
    """
    if platform.system() != "Linux":
        return []
    out = _run_probe(["lspci", "-nn"])
    if not out:
        return []
    wanted = ("vga compatible controller", "3d controller", "display controller")
    return [ln for ln in out.splitlines()
            if any(w in ln.lower() for w in wanted)]


def _vendor_from_lspci_line(line: str) -> str:
    """Map an lspci ``-nn`` line to ``nvidia``/``amd``/``intel``/``unknown``.

    Prefers the bracketed vendor ID (``[8086:...]``) — stable across
    locales — then falls back to the human-readable vendor string.
    """
    low = line.lower()
    # Bracketed [VEN:DEV] ids are the robust signal.
    if f"[{_VENDOR_ID_INTEL}:" in low:
        return "intel"
    if f"[{_VENDOR_ID_NVIDIA}:" in low:
        return "nvidia"
    if f"[{_VENDOR_ID_AMD}:" in low:
        return "amd"
    # Fallback: human-readable vendor substrings.
    if "intel" in low:
        return "intel"
    if "nvidia" in low:
        return "nvidia"
    if "amd" in low or "ati" in low or "advanced micro devices" in low:
        return "amd"
    return "unknown"


def _pci_bus_from_lspci_line(line: str) -> str:
    """Extract the leading PCI bus token (``03:00.0``) from an lspci line."""
    m = re.match(r"^([0-9a-fA-F]{2,4}:[0-9a-fA-F]{2}\.[0-9a-fA-F])", line.strip())
    if m:
        return _normalize_pci_bus(m.group(1))
    return ""


def _bus_is_integrated(pci_bus: str) -> Optional[bool]:
    """Classify a PCI bus token as integrated / discrete / unknown.

    Integrated graphics share the CPU root complex → bus ``00:`` (Intel
    iGPUs classically at ``00:02.x``; AMD APU iGPUs also enumerate on the
    root bus). Discrete cards live behind a PCIe root port → bus ``01:``,
    ``03:``, ``0a:`` etc.

    Returns ``True`` (integrated), ``False`` (discrete), or ``None`` when
    the bus token is empty / unrecognized (caller applies keep-on-uncertainty).
    """
    if not pci_bus:
        return None
    bus = pci_bus.split(":", 1)[0]
    if len(bus) > 2:
        # domain-qualified somehow slipped through; take the last 2 hex.
        bus = bus[-2:]
    try:
        bus_num = int(bus, 16)
    except ValueError:
        return None
    return bus_num == 0


def _enumerate_intel_lspci(vga_lines: list[str], known_buses: set[str]) -> list[GpuCandidate]:
    """Build Intel candidates from lspci lines not already claimed.

    Intel GPUs are invisible to nvidia-smi/rocm-smi, so lspci is the only
    way to SEE them and explicitly exclude them (closes the historical
    "Intel falls to CPU by accident" gap). ``known_buses`` holds buses
    already represented by NVIDIA/AMD candidates so we don't double-count.
    """
    cands: list[GpuCandidate] = []
    for line in vga_lines:
        if _vendor_from_lspci_line(line) != "intel":
            continue
        bus = _pci_bus_from_lspci_line(line)
        if bus and bus in known_buses:
            continue
        integrated = _bus_is_integrated(bus)
        cands.append(GpuCandidate(
            vendor="intel",
            name=_lspci_device_name(line),
            vram_gb=0.0,
            # Intel display adapters are integrated unless the bus says
            # otherwise; either way Intel is dropped downstream.
            is_integrated=True if integrated is None else integrated,
            pci_bus=bus,
        ))
    return cands


def _lspci_device_name(line: str) -> str:
    """Best-effort device name from an lspci ``-nn`` line (after the class)."""
    # Format: "03:00.0 VGA compatible controller [0300]: <Vendor> <Device> [VEN:DEV] (rev ..)"
    after_class = line.split(":", 2)
    if len(after_class) >= 3:
        tail = after_class[2]
        # Drop a leading class label up to the first "]:" if present.
        if "]:" in tail:
            tail = tail.split("]:", 1)[1]
        # Strip trailing [VEN:DEV] and (rev ..) noise.
        tail = re.sub(r"\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]", "", tail)
        tail = re.sub(r"\(rev [0-9a-fA-F]+\)", "", tail)
        return tail.strip()
    return ""


def _classify_amd_integrated(
    cand: GpuCandidate, vga_lines: list[str]
) -> GpuCandidate:
    """Refine an AMD candidate's ``is_integrated`` + ``pci_bus`` via lspci.

    rocm-smi can't reliably distinguish an AMD APU iGPU from a discrete
    Radeon. We cross-reference lspci:

    1. If lspci shows an AMD VGA/3D device on the root bus (``00:``) and a
       SEPARATE AMD device on a PCIe root port, the root-bus one is the
       iGPU. We can't perfectly pair rocm-smi rows to lspci lines across
       versions, so we use VRAM as the pairing signal: a UMA-sized
       (<= ~2 GB) AMD card co-located with an AMD root-bus VGA line is the
       iGPU.
    2. Backstop (no lspci): a sub-UMA-ceiling VRAM AMD card is treated as
       integrated ONLY when there is ALSO a plausibly-discrete AMD card in
       the set — i.e. we never demote a host's *only* AMD card to iGPU
       (keep-on-uncertainty; a single small AMD card stays usable-discrete
       and the VRAM threshold in _decide_gpu_mode handles it).

    NOTE: This function only ANNOTATES; the actual drop happens in
    :func:`select_gpu_device`. Annotating (not dropping) keeps the
    enumeration honest for the GUI ("found iGPU + discrete").
    """
    # Find AMD root-bus VGA lines (candidate iGPU buses).
    amd_root_bus = any(
        _vendor_from_lspci_line(ln) == "amd"
        and _bus_is_integrated(_pci_bus_from_lspci_line(ln)) is True
        for ln in vga_lines
    )
    amd_discrete_bus = any(
        _vendor_from_lspci_line(ln) == "amd"
        and _bus_is_integrated(_pci_bus_from_lspci_line(ln)) is False
        for ln in vga_lines
    )
    # If lspci positively shows BOTH an AMD iGPU (root bus) AND an AMD
    # discrete card, a UMA-sized rocm-smi row is the iGPU.
    if (
        amd_root_bus
        and amd_discrete_bus
        and 0.0 < cand.vram_gb <= _IGPU_UMA_VRAM_CEILING_GB
    ):
        return GpuCandidate(
            vendor=cand.vendor, name=cand.name, vram_gb=cand.vram_gb,
            is_integrated=True, pci_bus=cand.pci_bus,
        )
    return cand


def enumerate_gpus() -> list[GpuCandidate]:
    """Probe every GPU subsystem and return classified candidates.

    Soft-fails to ``[]``; NEVER raises. The returned list may contain
    Intel + integrated candidates — they are RETAINED here (so callers /
    the GUI can report "found N GPUs, ignoring iGPU") and only filtered
    in :func:`select_gpu_device`.
    """
    vga_lines = _lspci_vga_lines()

    nvidia = _enumerate_nvidia()
    amd_raw = _enumerate_amd()
    # Refine AMD iGPU classification against lspci.
    amd = [_classify_amd_integrated(c, vga_lines) for c in amd_raw]

    known_buses = {c.pci_bus for c in (nvidia + amd) if c.pci_bus}
    intel = _enumerate_intel_lspci(vga_lines, known_buses)

    return [*nvidia, *amd, *intel]


# ---------------------------------------------------------------------------
# Decision layer (PURE — no probes, fully fixture-testable)
# ---------------------------------------------------------------------------

def select_gpu_device(
    candidates: list[GpuCandidate],
    *,
    vendor_pref: Optional[str] = None,
) -> Optional[GpuCandidate]:
    """Pick the most-capable usable discrete GPU, or ``None`` (CPU path).

    Algorithm (HW constraints, v0.2.68):
      1. Drop every Intel candidate (unsupported — iGPU or discrete).
      2. Drop every integrated candidate (iGPU is never a usable
         accelerator for the VCO model stack; always AMD/Intel).
      3. If ``vendor_pref`` (``"nvidia"`` / ``"amd"`` — from VCT_GPU_VENDOR)
         is set AND at least one usable candidate matches it, restrict the
         set to that vendor (preserves the explicit-override semantics).
         ``"metal"`` is handled by the caller BEFORE this function (Apple
         unified memory has no discrete-VRAM concept).
      4. From the remainder pick ``max(vram_gb)``. Ties → prefer NVIDIA
         (mature CUDA tooling; matches ``_decide_gpu_mode``'s fallback).
      5. Empty remainder → ``None`` → CPU path.

    PURE: no side effects, no probes. ``candidates`` is the output of
    :func:`enumerate_gpus` (or a synthetic fixture in tests).

    Keep-on-uncertainty: a candidate whose vendor is ``"unknown"`` is
    NOT Intel and (per :func:`enumerate_gpus`) defaults ``is_integrated``
    to ``False``, so it survives both filters and competes as discrete —
    we never strand a card we merely failed to classify.
    """
    pref = (vendor_pref or "").strip().lower() or None

    # Steps 1-2: usable = not Intel, not integrated.
    usable = [
        c for c in candidates
        if c.vendor != "intel" and not c.is_integrated
    ]
    if not usable:
        return None

    # Step 3: honor a usable vendor preference.
    if pref in ("nvidia", "amd"):
        pref_matches = [c for c in usable if c.vendor == pref]
        if pref_matches:
            usable = pref_matches
        # If the preferred vendor has no usable card, fall through to the
        # full usable set (lenient — never strand the install on a pref
        # the hardware can't satisfy; mirrors gpu_profile's fall-through).

    # Step 4: max VRAM; ties prefer NVIDIA.
    def _rank(c: GpuCandidate) -> tuple[float, int]:
        return (c.vram_gb, 1 if c.vendor == "nvidia" else 0)

    return max(usable, key=_rank)
