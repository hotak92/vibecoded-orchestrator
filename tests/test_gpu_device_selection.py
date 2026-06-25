# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.68 Defect Y — multi-GPU / iGPU-aware GPU device selection.

Three test surfaces:

1. PURE decision tests over synthetic ``GpuCandidate`` lists into
   :func:`vco_lib.gpu_device.select_gpu_device` — no subprocess mocking.
   These pin the HW-constraint rules (drop Intel, drop iGPU, pick
   max-VRAM discrete, ties prefer NVIDIA, keep-on-uncertainty).

2. PARSER unit tests for the thin probe layer — mock subprocess output
   strings (nvidia-smi / rocm-smi / lspci) and assert the candidates
   parse correctly + soft-fail to ``[]`` when tools are missing/hang.

3. The end-to-end CORRECTNESS INVARIANT (design doc §6): an
   AMD-iGPU + AMD-discrete host must select the discrete card's VRAM and
   route through the embedding ladder + AMD-swap to **qwen3, NEVER Jina**.
   Plus the regression that today's first-device probe would have
   produced the downgrade bug.

Single-GPU regression (the common case) is asserted explicitly: one
usable discrete card in → that exact card out, byte-identical VRAM.
"""
from __future__ import annotations

import subprocess

import pytest

from vco_lib.gpu_device import (
    GpuCandidate,
    enumerate_gpus,
    select_gpu_device,
    _enumerate_nvidia,
    _enumerate_amd,
    _vendor_from_lspci_line,
    _pci_bus_from_lspci_line,
    _bus_is_integrated,
)
from vco_lib.embedding_selection import (
    select_code_embedding_backend,
    _CODE_BACKEND_CODESAGE,
    _CODE_BACKEND_QWEN3,
    _CODE_BACKEND_JINA,
)


# ---------------------------------------------------------------------------
# 1. PURE select_gpu_device — synthetic candidate lists
# ---------------------------------------------------------------------------

class TestSelectSingleGpuRegression:
    """The common case MUST be byte-identical to the old first-card probe."""

    def test_single_nvidia_discrete_unchanged(self):
        cands = [GpuCandidate("nvidia", "RTX 4080", 16.0, False)]
        chosen = select_gpu_device(cands)
        assert chosen is not None
        assert chosen.vendor == "nvidia"
        assert chosen.vram_gb == 16.0

    def test_single_amd_discrete_unchanged(self):
        cands = [GpuCandidate("amd", "Radeon RX 7900", 16.0, False)]
        chosen = select_gpu_device(cands)
        assert chosen is not None
        assert chosen.vendor == "amd"
        assert chosen.vram_gb == 16.0

    def test_single_amd_discrete_8gb_preserved(self):
        # An 8 GB single AMD card: chosen unchanged; the VRAM threshold in
        # _decide_gpu_mode (not this function) handles tiering.
        cands = [GpuCandidate("amd", "RX 6600", 8.0, False)]
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vram_gb == 8.0


class TestSelectMultiGpu:
    def test_dual_nvidia_picks_largest(self):
        cands = [
            GpuCandidate("nvidia", "RTX 3060", 8.0, False),
            GpuCandidate("nvidia", "RTX 4090", 24.0, False),
        ]
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vram_gb == 24.0

    def test_amd_igpu_plus_amd_discrete_picks_discrete(self):
        # THE INVARIANT: capable AMD discrete must NOT be under-tiered by
        # the iGPU's small UMA memory.
        cands = [
            GpuCandidate("amd", "Raphael iGPU", 0.5, True),
            GpuCandidate("amd", "Radeon RX 7900", 16.0, False),
        ]
        chosen = select_gpu_device(cands)
        assert chosen is not None
        assert chosen.vendor == "amd" and chosen.vram_gb == 16.0

    def test_amd_igpu_plus_nvidia_discrete_picks_nvidia(self):
        cands = [
            GpuCandidate("amd", "Cezanne iGPU", 1.0, True),
            GpuCandidate("nvidia", "RTX 4070", 12.0, False),
        ]
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vendor == "nvidia"

    def test_intel_igpu_plus_nvidia_discrete_excludes_intel(self):
        cands = [
            GpuCandidate("intel", "UHD Graphics 770", 0.0, True),
            GpuCandidate("nvidia", "RTX 4080", 16.0, False),
        ]
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vendor == "nvidia"

    def test_tie_prefers_nvidia(self):
        cands = [
            GpuCandidate("amd", "RX 7900", 16.0, False),
            GpuCandidate("nvidia", "RTX 4080", 16.0, False),
        ]
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vendor == "nvidia"


class TestSelectCpuPath:
    def test_intel_only_falls_to_cpu(self):
        # Intel discrete (e.g. Arc) is unsupported → CPU path.
        cands = [GpuCandidate("intel", "Arc A770", 16.0, False)]
        assert select_gpu_device(cands) is None

    def test_intel_igpu_only_falls_to_cpu(self):
        cands = [GpuCandidate("intel", "UHD 630", 0.0, True)]
        assert select_gpu_device(cands) is None

    def test_no_gpu_falls_to_cpu(self):
        assert select_gpu_device([]) is None

    def test_only_integrated_falls_to_cpu(self):
        # An AMD APU box with NO discrete card → iGPU dropped → CPU.
        cands = [GpuCandidate("amd", "Phoenix iGPU", 2.0, True)]
        assert select_gpu_device(cands) is None


class TestSelectKeepOnUncertainty:
    def test_unknown_vendor_kept_as_discrete(self):
        # Classification failed (vendor unknown) — keep the card rather
        # than strand a GPU the user knows is there.
        cands = [GpuCandidate("unknown", "Mystery GPU", 12.0, False)]
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vendor == "unknown"

    def test_unknown_competes_against_known(self):
        cands = [
            GpuCandidate("unknown", "Mystery", 8.0, False),
            GpuCandidate("nvidia", "RTX 4090", 24.0, False),
        ]
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vendor == "nvidia"


class TestSelectVendorPref:
    def test_vendor_pref_amd_restricts(self):
        cands = [
            GpuCandidate("nvidia", "RTX 4080", 16.0, False),
            GpuCandidate("amd", "RX 7900", 12.0, False),
        ]
        chosen = select_gpu_device(cands, vendor_pref="amd")
        assert chosen is not None and chosen.vendor == "amd"

    def test_vendor_pref_nvidia_restricts(self):
        cands = [
            GpuCandidate("nvidia", "RTX 4070", 12.0, False),
            GpuCandidate("amd", "RX 7900", 24.0, False),
        ]
        chosen = select_gpu_device(cands, vendor_pref="nvidia")
        assert chosen is not None and chosen.vendor == "nvidia"

    def test_vendor_pref_unsatisfiable_falls_through(self):
        # Pref AMD but only NVIDIA usable → lenient: pick the NVIDIA card
        # rather than strand the install.
        cands = [GpuCandidate("nvidia", "RTX 4080", 16.0, False)]
        chosen = select_gpu_device(cands, vendor_pref="amd")
        assert chosen is not None and chosen.vendor == "nvidia"

    def test_vendor_pref_picks_largest_within_vendor(self):
        cands = [
            GpuCandidate("nvidia", "RTX 3060", 8.0, False),
            GpuCandidate("nvidia", "RTX 4090", 24.0, False),
            GpuCandidate("amd", "RX 7900", 16.0, False),
        ]
        chosen = select_gpu_device(cands, vendor_pref="nvidia")
        assert chosen is not None and chosen.vram_gb == 24.0


# ---------------------------------------------------------------------------
# 2. PARSER unit tests — mocked probe output
# ---------------------------------------------------------------------------

class _FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def _mock_probe(monkeypatch, *, present: dict[str, str], outputs: dict[str, str]):
    """Patch shutil.which + subprocess.run in gpu_device.

    ``present`` maps tool-name → "yes" for tools on PATH; ``outputs`` maps
    tool-name → stdout the tool returns (exit 0).
    """
    import vco_lib.gpu_device as gd

    def fake_which(tool):
        return f"/usr/bin/{tool}" if tool in present else None

    def fake_run(cmd, *a, **k):
        tool = cmd[0]
        if tool in outputs:
            return _FakeRun(stdout=outputs[tool], returncode=0)
        return _FakeRun(stdout="", returncode=1)

    monkeypatch.setattr(gd.shutil, "which", fake_which)
    monkeypatch.setattr(gd.subprocess, "run", fake_run)


class TestNvidiaParser:
    def test_single_nvidia_row(self, monkeypatch):
        out = "0, NVIDIA GeForce RTX 4080 SUPER, 16376, 00000000:01:00.0\n"
        _mock_probe(monkeypatch, present={"nvidia-smi": "y"},
                    outputs={"nvidia-smi": out})
        cands = _enumerate_nvidia()
        assert len(cands) == 1
        assert cands[0].vendor == "nvidia"
        assert cands[0].name == "NVIDIA GeForce RTX 4080 SUPER"
        assert cands[0].vram_gb == pytest.approx(15.99, abs=0.02)
        assert cands[0].is_integrated is False
        assert cands[0].pci_bus == "01:00.0"

    def test_dual_nvidia_rows(self, monkeypatch):
        out = (
            "0, NVIDIA RTX 3060, 12288, 00000000:01:00.0\n"
            "1, NVIDIA RTX 4090, 24576, 00000000:03:00.0\n"
        )
        _mock_probe(monkeypatch, present={"nvidia-smi": "y"},
                    outputs={"nvidia-smi": out})
        cands = _enumerate_nvidia()
        assert len(cands) == 2
        assert cands[0].vram_gb == pytest.approx(12.0, abs=0.02)
        assert cands[1].vram_gb == pytest.approx(24.0, abs=0.02)

    def test_missing_nvidia_smi_returns_empty(self, monkeypatch):
        _mock_probe(monkeypatch, present={}, outputs={})
        assert _enumerate_nvidia() == []


class TestAmdParser:
    def test_amd_csv_two_cards(self, monkeypatch):
        out = (
            "device,VRAM Total Memory (B)\n"
            "card0,536870912\n"        # iGPU UMA ~0.5 GB
            "card1,17179869184\n"      # discrete 16 GB
        )
        _mock_probe(monkeypatch, present={"rocm-smi": "y"},
                    outputs={"rocm-smi": out})
        cands = _enumerate_amd()
        assert len(cands) == 2
        assert cands[0].vram_gb == pytest.approx(0.5, abs=0.01)
        assert cands[1].vram_gb == pytest.approx(16.0, abs=0.01)
        assert all(c.vendor == "amd" for c in cands)

    def test_missing_rocm_smi_returns_empty(self, monkeypatch):
        _mock_probe(monkeypatch, present={}, outputs={})
        assert _enumerate_amd() == []


class TestProbeSoftFail:
    def test_run_probe_timeout_soft_fails(self, monkeypatch):
        import vco_lib.gpu_device as gd

        monkeypatch.setattr(gd.shutil, "which", lambda t: "/usr/bin/" + t)

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=10)

        monkeypatch.setattr(gd.subprocess, "run", boom)
        # Whole enumeration must not raise.
        assert _enumerate_nvidia() == []

    def test_enumerate_gpus_never_raises_on_oserror(self, monkeypatch):
        import vco_lib.gpu_device as gd

        monkeypatch.setattr(gd.shutil, "which", lambda t: "/usr/bin/" + t)

        def boom(*a, **k):
            raise OSError("exec format error")

        monkeypatch.setattr(gd.subprocess, "run", boom)
        assert enumerate_gpus() == []


class TestLspciHelpers:
    NVIDIA_LINE = (
        "03:00.0 VGA compatible controller [0300]: NVIDIA Corporation "
        "AD103 [GeForce RTX 4080] [10de:2704] (rev a1)"
    )
    INTEL_IGPU_LINE = (
        "00:02.0 VGA compatible controller [0300]: Intel Corporation "
        "AlderLake-S GT1 [8086:4680] (rev 0c)"
    )
    AMD_IGPU_LINE = (
        "00:08.0 VGA compatible controller [0300]: Advanced Micro Devices, "
        "Inc. [AMD/ATI] Raphael [1002:164e] (rev cb)"
    )
    AMD_DISCRETE_LINE = (
        "03:00.0 VGA compatible controller [0300]: Advanced Micro Devices, "
        "Inc. [AMD/ATI] Navi 31 [Radeon RX 7900 XTX] [1002:744c] (rev c8)"
    )

    def test_vendor_from_ids(self):
        assert _vendor_from_lspci_line(self.NVIDIA_LINE) == "nvidia"
        assert _vendor_from_lspci_line(self.INTEL_IGPU_LINE) == "intel"
        assert _vendor_from_lspci_line(self.AMD_IGPU_LINE) == "amd"
        assert _vendor_from_lspci_line(self.AMD_DISCRETE_LINE) == "amd"

    def test_bus_extraction(self):
        assert _pci_bus_from_lspci_line(self.NVIDIA_LINE) == "03:00.0"
        assert _pci_bus_from_lspci_line(self.INTEL_IGPU_LINE) == "00:02.0"

    def test_bus_classification(self):
        assert _bus_is_integrated("00:02.0") is True
        assert _bus_is_integrated("00:08.0") is True
        assert _bus_is_integrated("03:00.0") is False
        assert _bus_is_integrated("01:00.0") is False
        assert _bus_is_integrated("") is None


class TestEnumerateIntegration:
    def test_intel_igpu_plus_nvidia_discrete_full_enumeration(self, monkeypatch):
        # nvidia-smi sees the discrete card; lspci sees the Intel iGPU.
        nvidia_out = "0, NVIDIA RTX 4080, 16376, 00000000:03:00.0\n"
        lspci_out = (
            "00:02.0 VGA compatible controller [0300]: Intel Corporation "
            "UHD Graphics [8086:4680] (rev 0c)\n"
            "03:00.0 VGA compatible controller [0300]: NVIDIA Corporation "
            "AD103 [10de:2704] (rev a1)\n"
        )
        _mock_probe(
            monkeypatch,
            present={"nvidia-smi": "y", "lspci": "y"},
            outputs={"nvidia-smi": nvidia_out, "lspci": lspci_out},
        )
        # Force Linux so lspci runs.
        monkeypatch.setattr("vco_lib.gpu_device.platform.system", lambda: "Linux")
        cands = enumerate_gpus()
        vendors = sorted(c.vendor for c in cands)
        assert "nvidia" in vendors and "intel" in vendors
        # The Intel iGPU must be excluded; NVIDIA chosen.
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vendor == "nvidia"

    def test_amd_igpu_plus_amd_discrete_classified_via_lspci(self, monkeypatch):
        # rocm-smi enumerates BOTH AMD cards (iGPU first, UMA-sized);
        # lspci shows the iGPU on the root bus + discrete on a root port.
        rocm_out = (
            "device,VRAM Total Memory (B)\n"
            "card0,536870912\n"         # 0.5 GB iGPU
            "card1,25769803776\n"       # 24 GB discrete
        )
        lspci_out = (
            "00:08.0 VGA compatible controller [0300]: Advanced Micro "
            "Devices, Inc. [AMD/ATI] Raphael [1002:164e] (rev cb)\n"
            "03:00.0 VGA compatible controller [0300]: Advanced Micro "
            "Devices, Inc. [AMD/ATI] Navi 31 [1002:744c] (rev c8)\n"
        )
        _mock_probe(
            monkeypatch,
            present={"rocm-smi": "y", "lspci": "y"},
            outputs={"rocm-smi": rocm_out, "lspci": lspci_out},
        )
        monkeypatch.setattr("vco_lib.gpu_device.platform.system", lambda: "Linux")
        cands = enumerate_gpus()
        # The 0.5 GB AMD card co-located with an AMD root-bus VGA line +
        # an AMD discrete-bus line must be flagged integrated.
        igpu = [c for c in cands if c.vram_gb < 1.0]
        assert igpu and igpu[0].is_integrated is True
        chosen = select_gpu_device(cands)
        assert chosen is not None and chosen.vram_gb == pytest.approx(24.0, abs=0.01)


# ---------------------------------------------------------------------------
# 3. END-TO-END CORRECTNESS INVARIANT (design doc §6)
# ---------------------------------------------------------------------------

class TestAmdCapableQwen3NotJinaInvariant:
    """An AMD discrete card capable of CodeSage MUST route to qwen3,
    NEVER Jina. The bug was bad VRAM INPUT (iGPU memory), not the ladder.
    """

    def _ladder_then_amd_swap(
        self, vendor: str, vram_gb: float,
        *, ram_gb: float = 32.0, cores: int = 16,
    ) -> str:
        """Reproduce install.py's _choose_embedding_config code-pick +
        AMD→qwen3 swap exactly (install.py:9925 + 9952).
        """
        pick = select_code_embedding_backend(
            gpu_vram_gb=vram_gb, ram_gb=ram_gb, cores=cores,
            openai_key_available=False,
        )
        # has_gpu is True (a usable discrete card was chosen).
        if pick == _CODE_BACKEND_CODESAGE and vendor == "amd":
            pick = _CODE_BACKEND_QWEN3
        return pick

    def test_amd_igpu_plus_discrete_picks_discrete_then_qwen3_not_jina(self):
        # Modest-RAM host (16 GB / 6 cores) so the CPU-tier fallback does
        # NOT mask the bug: only the discrete card's 16 GB VRAM gets us off
        # the Jina floor. This is the structurally-worst AMD case.
        cands = [
            GpuCandidate("amd", "Raphael iGPU", 0.5, True),
            GpuCandidate("amd", "Radeon RX 7900", 16.0, False),
        ]
        chosen = select_gpu_device(cands)
        assert chosen is not None
        assert chosen.vendor == "amd" and chosen.vram_gb == 16.0

        pick = self._ladder_then_amd_swap(
            chosen.vendor, chosen.vram_gb, ram_gb=16.0, cores=6,
        )
        assert pick == _CODE_BACKEND_QWEN3
        assert pick != _CODE_BACKEND_JINA  # the downgrade bug

    def test_old_first_device_probe_would_have_produced_jina(self):
        # Regression guard: with TODAY's first-device probe the SAME host
        # (16 GB RAM / 6 cores) yields vram_gb=0.5 → CPU tier doesn't meet
        # the qwen3 RAM/cores bar → Jina (the downgrade bug the fix closes).
        downgrade = self._ladder_then_amd_swap("amd", 0.5, ram_gb=16.0, cores=6)
        assert downgrade == _CODE_BACKEND_JINA  # proves the bug existed

    def test_amd_discrete_6_to_12_gb_qwen3_directly(self):
        # A 10 GB discrete AMD card → qwen3 directly (ladder 6-12 GB band).
        cands = [GpuCandidate("amd", "RX 6700 XT", 10.0, False)]
        chosen = select_gpu_device(cands)
        assert chosen is not None
        pick = self._ladder_then_amd_swap(chosen.vendor, chosen.vram_gb)
        assert pick == _CODE_BACKEND_QWEN3

    def test_genuine_small_amd_discrete_still_jina(self):
        # A genuine 4 GB AMD discrete card → Jina (correct floor; the fix
        # must NOT over-promote a truly small card).
        cands = [GpuCandidate("amd", "RX 6400", 4.0, False)]
        chosen = select_gpu_device(cands)
        assert chosen is not None
        pick = self._ladder_then_amd_swap(chosen.vendor, chosen.vram_gb)
        assert pick == _CODE_BACKEND_JINA

    def test_nvidia_discrete_keeps_codesage(self):
        # Sanity: an NVIDIA 16 GB card keeps CodeSage (no AMD swap).
        cands = [GpuCandidate("nvidia", "RTX 4080", 16.0, False)]
        chosen = select_gpu_device(cands)
        assert chosen is not None
        pick = self._ladder_then_amd_swap(chosen.vendor, chosen.vram_gb)
        assert pick == _CODE_BACKEND_CODESAGE
