# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 VibeCoded Tools
"""Opt-in hardware profiling.

Runs at most once every 7 days (cached in ~/.vibecoded/hardware.json).
Never raises — missing tools return a partial dict. No personally
identifying information (hostnames, usernames, MAC addresses) is ever
collected here.
"""
from __future__ import annotations

import json
import logging
import platform
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".vibecoded"
CACHE_FILE = CACHE_DIR / "hardware.json"

REFRESH_SECONDS = 7 * 24 * 3600  # 1 week


def _run(cmd: List[str], timeout: float = 2.0) -> Optional[str]:
    """Run a subprocess and return stdout, or None on any failure."""
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if res.returncode != 0:
            return None
        return res.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.debug("Hardware probe %s failed: %s", cmd[0] if cmd else "?", e)
        return None


def _detect_cpu() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "arch": platform.machine() or "",
        "system": platform.system() or "",
        "model": "",
    }
    # platform.processor() is often empty on Linux; fall back to /proc/cpuinfo.
    model = platform.processor() or ""
    if not model and platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        _, _, val = line.partition(":")
                        model = val.strip()
                        break
        except OSError:
            pass
    if not model and platform.system() == "Darwin":
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out:
            model = out.strip()
    info["model"] = model
    try:
        import os as _os
        info["logical_cores"] = _os.cpu_count() or 0
    except Exception:
        info["logical_cores"] = 0
    return info


def _detect_ram() -> Dict[str, Any]:
    out: Dict[str, Any] = {"gb": 0.0, "speed_mhz": None}
    try:
        import psutil  # type: ignore[import-not-found]
        out["gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 2)
    except Exception as e:
        log.debug("psutil RAM probe failed: %s", e)
    # Speed is hard to get portably; try dmidecode on Linux (usually needs root).
    if platform.system() == "Linux":
        raw = _run(["dmidecode", "-t", "memory"], timeout=1.5)
        if raw:
            for line in raw.splitlines():
                line = line.strip()
                if line.lower().startswith("speed:") and "mhz" in line.lower():
                    # Take first populated value like "Speed: 3200 MT/s"
                    parts = line.split()
                    for p in parts:
                        if p.isdigit():
                            out["speed_mhz"] = int(p)
                            break
                    if out["speed_mhz"]:
                        break
    return out


def _detect_gpus() -> List[Dict[str, Any]]:
    gpus: List[Dict[str, Any]] = []

    # NVIDIA
    nv = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    if nv:
        for line in nv.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if parts and parts[0]:
                gpu: Dict[str, Any] = {"vendor": "nvidia", "name": parts[0]}
                if len(parts) > 1:
                    gpu["vram"] = parts[1]
                gpus.append(gpu)

    # AMD ROCm
    if not gpus:
        rocm = _run(["rocm-smi", "--showproductname"])
        if rocm:
            for line in rocm.splitlines():
                line = line.strip()
                if "card series" in line.lower() or "product name" in line.lower():
                    _, _, val = line.partition(":")
                    name = val.strip()
                    if name:
                        gpus.append({"vendor": "amd", "name": name})

    # Apple Silicon
    if not gpus and platform.system() == "Darwin":
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out and ("Apple" in out):
            gpus.append({"vendor": "apple", "name": out.strip(), "integrated": True})

    return gpus


def _load_cache() -> Optional[Dict[str, Any]]:
    if not CACHE_FILE.exists():
        return None
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        ts = float(data.get("_cached_at", 0))
        if (time.time() - ts) < REFRESH_SECONDS:
            return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        log.debug("Hardware cache read failed: %s", e)
    return None


def _save_cache(data: Dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with CACHE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
    except OSError as e:
        log.debug("Hardware cache write failed: %s", e)


def detect_hardware(*, use_cache: bool = True) -> Dict[str, Any]:
    """Return a dict with CPU, RAM, and GPU info.

    Cached for 7 days. Never raises. Missing probes return partial data.
    """
    if use_cache:
        cached = _load_cache()
        if cached is not None:
            return cached

    data: Dict[str, Any] = {
        "_cached_at": time.time(),
        "cpu": _detect_cpu(),
        "ram": _detect_ram(),
        "gpus": _detect_gpus(),
    }
    _save_cache(data)
    return data
