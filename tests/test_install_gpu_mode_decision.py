"""Tests for `install._decide_gpu_mode` (v0.2.9 Bug K; v0.2.20 AMD/ROCm split).

The Python decision function MUST stay in lockstep with the Rust side
(`launcher::commands::gpu_policy::decide_gpu_mode`). The two are tested
in their respective harnesses against the same matrix — if either side
diverges, the launcher's reconfig flow disagrees with install.py and the
user gets a flag mismatch on `--update`.

Precedence (must hold in BOTH implementations):
  1. user_override (when not None) wins.
     - True:  vendor==metal → "metal"; vendor==amd → "rocm";
       everyone else (nvidia or empty) → "cuda".
     - False: "cpu".
  2. vendor==metal → "metal" (Apple Silicon — unified memory).
  3. vendor==nvidia AND vram >= threshold → "cuda".
  4. vendor==amd AND vram >= threshold → "rocm".
  5. Else → "cpu".

The 8 GB threshold is the default but is configurable per call. See
`install._DEFAULT_GPU_VRAM_THRESHOLD_GB` for the rationale.
"""

from __future__ import annotations

import pytest

from install import _DEFAULT_GPU_VRAM_THRESHOLD_GB, _decide_gpu_mode


def test_default_threshold_matches_rust_side():
    """Pin the default so the cross-language sync is enforced. Rust uses
    `DEFAULT_GPU_VRAM_THRESHOLD_GB = 8.0`."""
    assert _DEFAULT_GPU_VRAM_THRESHOLD_GB == 8.0


# ---------------------------------------------------------------------------
# Threshold behaviour — auto mode (no user override)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vram, expected",
    [
        (0.0, "cpu"),     # probe failed
        (2.0, "cpu"),     # tiny card
        (4.0, "cpu"),     # below threshold
        (7.99, "cpu"),    # just under
        (8.0, "cuda"),    # inclusive threshold
        (8.01, "cuda"),   # just over
        (12.0, "cuda"),   # comfortable
        (24.0, "cuda"),   # plenty
    ],
)
def test_nvidia_vram_threshold_auto(vram, expected):
    assert _decide_gpu_mode(vram_gb=vram, vendor="nvidia") == expected


@pytest.mark.parametrize(
    "vram, expected",
    [
        (0.0, "cpu"),
        (4.0, "cpu"),
        (8.0, "rocm"),
        (16.0, "rocm"),
    ],
)
def test_amd_vram_threshold_auto(vram, expected):
    """AMD follows the same VRAM threshold as NVIDIA but routes to the
    ROCm overlay instead of CUDA. v0.2.20 split the return value."""
    assert _decide_gpu_mode(vram_gb=vram, vendor="amd") == expected


# ---------------------------------------------------------------------------
# No discrete GPU vendor → CPU
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vram", [0.0, 8.0, 16.0])
def test_no_vendor_is_cpu(vram):
    """vendor="" (empty / unknown) always maps to CPU, even with a phantom
    VRAM number from a confused probe."""
    assert _decide_gpu_mode(vram_gb=vram, vendor="") == "cpu"


# ---------------------------------------------------------------------------
# Apple Silicon → Metal (no threshold)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vram", [0.0, 8.0, 32.0])
def test_apple_silicon_always_metal(vram):
    """Apple Silicon has unified memory — the VRAM number doesn't apply.
    Always return Metal in auto mode."""
    assert _decide_gpu_mode(vram_gb=vram, vendor="metal") == "metal"


# ---------------------------------------------------------------------------
# User override precedence
# ---------------------------------------------------------------------------


def test_override_true_forces_cuda_below_threshold_nvidia():
    """--gpu trusts the user — even on a 2 GB card. NVIDIA → cuda."""
    assert _decide_gpu_mode(
        vram_gb=2.0, vendor="nvidia", user_override=True
    ) == "cuda"


def test_override_true_forces_rocm_below_threshold_amd():
    """--gpu trusts the user — even on a 2 GB AMD card → rocm."""
    assert _decide_gpu_mode(
        vram_gb=2.0, vendor="amd", user_override=True
    ) == "rocm"


def test_override_true_with_no_vendor_falls_back_to_cuda():
    """User explicitly opted in — driver probe missed it, but we trust
    them. No vendor info → default to CUDA (the more common path; CUDA
    tooling is more mature than ROCm)."""
    assert _decide_gpu_mode(
        vram_gb=0.0, vendor="", user_override=True
    ) == "cuda"


def test_override_false_forces_cpu_with_24gb_card():
    """--cpu-only beats any auto detection. Useful for debugging or for
    avoiding a flaky GPU on shared dev hosts."""
    assert _decide_gpu_mode(
        vram_gb=24.0, vendor="nvidia", user_override=False
    ) == "cpu"


def test_override_false_forces_cpu_amd():
    """--cpu-only beats AMD detection too."""
    assert _decide_gpu_mode(
        vram_gb=16.0, vendor="amd", user_override=False
    ) == "cpu"


def test_override_true_on_apple_silicon_stays_metal():
    """An Apple Silicon owner passing `--gpu` can't get CUDA — they get
    Metal. We refuse to mis-classify (would route to a missing compose
    overlay)."""
    assert _decide_gpu_mode(
        vram_gb=0.0, vendor="metal", user_override=True
    ) == "metal"


def test_override_false_on_apple_silicon_yields_cpu():
    """User --cpu-only on Apple Silicon → CPU (explicit opt-out beats
    auto-Metal pick)."""
    assert _decide_gpu_mode(
        vram_gb=0.0, vendor="metal", user_override=False
    ) == "cpu"


# ---------------------------------------------------------------------------
# Custom threshold (per-module VRAM threshold, v0.2.20)
# ---------------------------------------------------------------------------


def test_custom_threshold_lower_qualifies_smaller_cards_nvidia():
    """User running a smaller model stack can lower the threshold."""
    assert _decide_gpu_mode(
        vram_gb=4.0, vendor="nvidia", threshold_gb=4.0
    ) == "cuda"
    assert _decide_gpu_mode(
        vram_gb=3.9, vendor="nvidia", threshold_gb=4.0
    ) == "cpu"


def test_custom_threshold_lower_qualifies_smaller_cards_amd():
    """AMD with a per-module 4 GB threshold (RL reranker case) qualifies
    on a 4 GB card."""
    assert _decide_gpu_mode(
        vram_gb=4.0, vendor="amd", threshold_gb=4.0
    ) == "rocm"
    assert _decide_gpu_mode(
        vram_gb=3.9, vendor="amd", threshold_gb=4.0
    ) == "cpu"


def test_custom_threshold_higher_rejects_8gb_card():
    """User running a bigger stack can raise the threshold and force a
    weak GPU to CPU."""
    assert _decide_gpu_mode(
        vram_gb=8.0, vendor="nvidia", threshold_gb=16.0
    ) == "cpu"
    assert _decide_gpu_mode(
        vram_gb=16.0, vendor="nvidia", threshold_gb=16.0
    ) == "cuda"


# ---------------------------------------------------------------------------
# Probe-failed edge cases (defensive)
# ---------------------------------------------------------------------------


def test_vendor_known_but_vram_probe_failed_is_cpu():
    """The probe reported a vendor (e.g. nvidia-smi exited 0 with the
    name but the memory query failed) → VRAM is 0. Conservative path:
    don't enable the GPU overlay against an unknown card."""
    assert _decide_gpu_mode(vram_gb=0.0, vendor="nvidia") == "cpu"
    assert _decide_gpu_mode(vram_gb=0.0, vendor="amd") == "cpu"


def test_unknown_vendor_string_is_cpu():
    """Defensive against future vendor strings — if the probe layer
    starts reporting something unexpected, fall back to CPU."""
    assert _decide_gpu_mode(vram_gb=16.0, vendor="intel-arc") == "cpu"


# ---------------------------------------------------------------------------
# Wire-string sync with Rust GpuMode (v0.2.20)
# ---------------------------------------------------------------------------


def test_return_values_match_rust_gpumode_serde():
    """Pin the four possible return values to the lowercase serde of
    Rust's `GpuMode` enum (cuda | rocm | metal | cpu). Renaming any
    of these breaks cross-process sync with the launcher."""
    valid = {"cuda", "rocm", "metal", "cpu"}
    # Sample one representative call per branch.
    assert _decide_gpu_mode(16.0, "nvidia") in valid
    assert _decide_gpu_mode(16.0, "amd") in valid
    assert _decide_gpu_mode(0.0, "metal") in valid
    assert _decide_gpu_mode(0.0, "") in valid
