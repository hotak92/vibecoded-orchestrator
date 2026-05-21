# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.23 C10 consolidation regression — pin the contract that
`_inference_models_for_capability` ALWAYS includes the model that
`select_summary_backend` will pick at runtime.

Pre-consolidation (v0.2.22 and earlier), the two functions kept
SEPARATE VRAM/RAM thresholds (inference: vram >= 7.5 / 5.0; summary:
vram >= 16.0 / 6.0) which meant some hosts pulled qwen3.5:9b
(via inference selector saying yes) but never used it (summary
selector picks gemma at sub-16 GB). Wasted bandwidth + disk.

Post-consolidation, the pull list is DERIVED from the summary
selector — same source of truth, no possible drift. This test
locks the invariant.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # noqa: E402
from install import (  # noqa: E402
    SystemInfo,
    _SUMMARY_BACKEND_GEMMA,
    _SUMMARY_BACKEND_QWEN35_9B,
    _inference_models_for_capability,
    select_summary_backend,
)


def _sysinfo(has_gpu: bool, vram_gb: float, ram_gb: float) -> SystemInfo:
    return SystemInfo(
        os_name="Linux",
        has_gpu=has_gpu,
        has_metal=False,
        container_cmd="podman",
        gpu_name="RTX 4090" if has_gpu else "",
        vram_gb=vram_gb,
        ram_gb=ram_gb,
        gpu_vendor="nvidia" if has_gpu else "",
    )


class PullListMatchesSummaryBackendTests(unittest.TestCase):
    """The pull list MUST include whatever the summary selector picks
    (when the selector picks a local model). Drift = a wasted pull
    OR a missing model at runtime."""

    def test_high_vram_pulls_qwen35_9b_matching_summary_pick(self):
        # 24 GB VRAM, 64 GB RAM, 8 cores → summary picks qwen3.5:9b
        sysinfo = _sysinfo(has_gpu=True, vram_gb=24.0, ram_gb=64.0)
        with patch.object(install, "_probe_cpu_cores", return_value=8):
            summary_pick = select_summary_backend(
                gpu_vram_gb=24.0, ram_gb=64.0, cores=8,
                claude_cli_available=False,
                openai_consent=False, openai_key_available=False,
            )
            pull_list = _inference_models_for_capability(sysinfo)
        self.assertEqual(summary_pick, _SUMMARY_BACKEND_QWEN35_9B)
        self.assertIn("qwen3.5:9b", pull_list)

    def test_mid_vram_pulls_gemma_matching_summary_pick(self):
        # 8 GB VRAM (below 16 GB qwen3.5:9b tier) → summary picks gemma
        sysinfo = _sysinfo(has_gpu=True, vram_gb=8.0, ram_gb=64.0)
        with patch.object(install, "_probe_cpu_cores", return_value=8):
            summary_pick = select_summary_backend(
                gpu_vram_gb=8.0, ram_gb=64.0, cores=8,
                claude_cli_available=False,
                openai_consent=False, openai_key_available=False,
            )
            pull_list = _inference_models_for_capability(sysinfo)
        self.assertEqual(summary_pick, _SUMMARY_BACKEND_GEMMA)
        self.assertIn("gemma4:e4b", pull_list)
        # Critical: qwen3.5:9b NOT pulled at 8 GB VRAM (would be wasted)
        self.assertNotIn("qwen3.5:9b", pull_list)

    def test_cpu_capable_pulls_gemma_matching_summary_pick(self):
        # CPU-only, 64 GB RAM, 8 cores → summary picks gemma (CPU path)
        sysinfo = _sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=64.0)
        with patch.object(install, "_probe_cpu_cores", return_value=8):
            summary_pick = select_summary_backend(
                gpu_vram_gb=0.0, ram_gb=64.0, cores=8,
                claude_cli_available=False,
                openai_consent=False, openai_key_available=False,
            )
            pull_list = _inference_models_for_capability(sysinfo)
        self.assertEqual(summary_pick, _SUMMARY_BACKEND_GEMMA)
        self.assertIn("gemma4:e4b", pull_list)
        # qwen3.5:9b NEVER on CPU path (16 GB VRAM tier only)
        self.assertNotIn("qwen3.5:9b", pull_list)

    def test_low_spec_cpu_pulls_only_floor(self):
        # CPU, 16 GB RAM, 4 cores → summary picks None (no local viable)
        sysinfo = _sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=16.0)
        with patch.object(install, "_probe_cpu_cores", return_value=4):
            summary_pick = select_summary_backend(
                gpu_vram_gb=0.0, ram_gb=16.0, cores=4,
                claude_cli_available=False,
                openai_consent=False, openai_key_available=False,
            )
            pull_list = _inference_models_for_capability(sysinfo)
        self.assertIsNone(summary_pick)
        # Floor model always pulled as universal fallback
        self.assertEqual(pull_list, ["qwen3.5:0.8b"])

    def test_floor_always_present(self):
        # Floor (qwen3.5:0.8b) ships in EVERY pull list regardless of tier.
        configurations = [
            (_sysinfo(has_gpu=True, vram_gb=24.0, ram_gb=64.0), 8),
            (_sysinfo(has_gpu=True, vram_gb=8.0, ram_gb=64.0), 8),
            (_sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=64.0), 8),
            (_sysinfo(has_gpu=False, vram_gb=0.0, ram_gb=16.0), 4),
            (_sysinfo(has_gpu=True, vram_gb=4.0, ram_gb=8.0), 2),
        ]
        for sysinfo, cores in configurations:
            with patch.object(install, "_probe_cpu_cores", return_value=cores):
                pull_list = _inference_models_for_capability(sysinfo)
            self.assertIn(
                "qwen3.5:0.8b", pull_list,
                f"floor missing from pull list for vram={sysinfo.vram_gb} "
                f"ram={sysinfo.ram_gb} cores={cores}",
            )


if __name__ == "__main__":
    unittest.main()
