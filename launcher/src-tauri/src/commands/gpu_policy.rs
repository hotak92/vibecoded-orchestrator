//! GPU mode decision policy (v0.2.9 Bug K; AMD/ROCm + per-module
//! threshold split out in v0.2.20).
//!
//! Pure decision function that maps (vram, vendor flags, user_override)
//! onto a `GpuMode` enum. The same logic lives in
//! `install.py::_decide_gpu_mode` (Python side); the two implementations
//! are kept in lock-step so the launcher's `apply_hardware_reconfig`
//! flow and the install-time decision arrive at the same answer for the
//! same inputs.
//!
//! ## Why a threshold?
//!
//! The orchestrator-core model stack is:
//!
//! | model                       | VRAM (rough) |
//! |-----------------------------|--------------|
//! | qwen3-embedding:0.6b        | ~1.2 GB      |
//! | CodeSage-Large-v2           | ~2.6 GB      |
//! | qwen3.5:9b (Q4)             | ~6.0 GB      |
//!
//! On a card with <8 GB VRAM, the inference model thrashes against the
//! embedders or fails to load — degrading to CPU is faster than partial
//! offload. 8 GB is the smallest VRAM that gives breathing room for the
//! whole stack. The threshold is configurable per-module via
//! `manifest.runtime.min_gpu_vram_gb` (v0.2.20) so smaller modules (e.g.
//! the RL reranker, which needs ~4 GB) can opt into GPU mode on hardware
//! that orchestrator-core would degrade to CPU.
//!
//! ## v0.2.20 — AMD/ROCm support
//!
//! Pre-v0.2.20 the enum had only `Gpu | Cpu | Metal` and `decide_gpu_mode`
//! accepted only `has_nvidia: bool` — AMD owners with sufficient VRAM
//! were silently routed to CPU. v0.2.20 splits the legacy `Gpu` into
//! `Cuda | Rocm` and adds a `has_amd: bool` argument. Precedence when
//! both NVIDIA and AMD are present: NVIDIA wins (rare workstation case;
//! CUDA tooling is more mature than ROCm tooling).
//!
//! ## Cross-platform
//!
//! Apple Silicon has unified memory, so a VRAM number doesn't apply
//! directly — `GpuMode::Metal` is its own arm. `has_apple_silicon: true`
//! always wins over the VRAM threshold AND over NVIDIA/AMD detection
//! (only relevant on dual-GPU Mac Pros).

/// Default VRAM threshold (GiB) below which we degrade to CPU-only mode.
///
/// Tuned for the orchestrator-core model stack — see module docstring.
/// Per-module overrides via `manifest.runtime.min_gpu_vram_gb` (v0.2.20).
/// Keep this in sync with `install.py::_DEFAULT_GPU_VRAM_THRESHOLD_GB`.
pub const DEFAULT_GPU_VRAM_THRESHOLD_GB: f64 = 8.0;

// v0.2.47: `GpuMode` relocated to `vct-launcher-core::services::gpu_mode`
// so the hub-side supervisor can resolve GPU variants the same way the
// launcher-side installer does (closes the variant-suffix gap in the
// supervisor start path — see
// knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md).
// The full `decide_gpu_mode` policy below stays here — it depends on the
// launcher-only `HardwareSnapshot` pipeline. Re-exported under the
// original name for source compatibility with the existing ~20 call
// sites that use `crate::commands::gpu_policy::GpuMode`.
pub use vct_launcher_core::services::gpu_mode::GpuMode;

/// Pure decision function.
///
/// Precedence:
///   1. `user_override` (when explicit) wins over everything.
///      - `user_override=true`:  Apple Silicon → Metal; else NVIDIA → Cuda;
///        else AMD → Rocm; else fallback Cuda (user accepted the tradeoff).
///      - `user_override=false`: Cpu (even with a 24 GB card).
///   2. Apple Silicon → `Metal`. No threshold check — unified memory.
///   3. NVIDIA present AND VRAM ≥ threshold → `Cuda`. NVIDIA wins over
///      AMD when both are detected (rare workstation case; CUDA tooling
///      is more mature).
///   4. AMD present AND VRAM ≥ threshold → `Rocm`.
///   5. Else → `Cpu`.
///
/// `vram_gb` of 0.0 with no vendor signals "no GPU detected" → `Cpu`.
/// `vram_gb` of 0.0 with a vendor signals "GPU detected but probe failed"
///   → `Cpu` (conservative — we'd rather not enable a GPU overlay against
///   an unknown card).
pub fn decide_gpu_mode(
    vram_gb: f64,
    has_nvidia: bool,
    has_amd: bool,
    has_apple_silicon: bool,
    user_override: Option<bool>,
    threshold_gb: f64,
) -> GpuMode {
    if let Some(force) = user_override {
        // User explicit choice always wins. Apple Silicon owners who pass
        // --gpu still get Metal (their hardware can't do CUDA); an NVIDIA
        // owner who passes --cpu-only gets CPU even with a 24 GB card.
        if force {
            if has_apple_silicon {
                return GpuMode::Metal;
            }
            if has_nvidia {
                return GpuMode::Cuda;
            }
            if has_amd {
                return GpuMode::Rocm;
            }
            // user_override=true with NO vendor detected — trust the
            // user (e.g. fresh driver install the probe missed) and
            // default to CUDA. Wrong-vendor-on-no-detection is rare;
            // the conservative fallback is the more common NVIDIA path.
            return GpuMode::Cuda;
        }
        return GpuMode::Cpu;
    }

    if has_apple_silicon {
        return GpuMode::Metal;
    }

    if has_nvidia && vram_gb >= threshold_gb {
        return GpuMode::Cuda;
    }

    if has_amd && vram_gb >= threshold_gb {
        return GpuMode::Rocm;
    }

    GpuMode::Cpu
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Insufficient VRAM → degrade to CPU even with a discrete GPU.
    #[test]
    fn vram_below_threshold_degrades_to_cpu() {
        assert_eq!(
            decide_gpu_mode(4.0, true, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
        assert_eq!(
            decide_gpu_mode(7.99, true, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// Threshold is inclusive — 8.0 GB qualifies for GPU.
    #[test]
    fn vram_at_threshold_inclusive_enables_gpu() {
        assert_eq!(
            decide_gpu_mode(8.0, true, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cuda
        );
    }

    /// Comfortable VRAM headroom — straightforward GPU enable.
    #[test]
    fn vram_well_above_threshold_enables_gpu() {
        assert_eq!(
            decide_gpu_mode(12.0, true, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cuda
        );
        assert_eq!(
            decide_gpu_mode(24.0, true, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cuda
        );
    }

    /// No NVIDIA + no AMD + no Apple Silicon → CPU no matter the VRAM.
    #[test]
    fn no_gpu_vendor_is_cpu() {
        assert_eq!(
            decide_gpu_mode(0.0, false, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
        // Bizarre but defensive: vram_gb set with no vendor (probe ambiguity)
        // — still CPU.
        assert_eq!(
            decide_gpu_mode(16.0, false, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// Apple Silicon always wins, regardless of VRAM number (unified memory
    /// — the `vram_gb` field doesn't apply directly).
    #[test]
    fn apple_silicon_returns_metal() {
        assert_eq!(
            decide_gpu_mode(0.0, false, false, true, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Metal
        );
        // Even with a phantom NVIDIA flag (shouldn't happen in practice
        // but defensively: Apple Silicon precedes vendor-discrete logic).
        assert_eq!(
            decide_gpu_mode(8.0, true, false, true, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Metal
        );
    }

    /// Explicit `--gpu` override forces GPU mode regardless of VRAM, on
    /// non-Apple machines.
    #[test]
    fn override_true_forces_gpu_below_threshold() {
        assert_eq!(
            decide_gpu_mode(2.0, true, false, false, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cuda
        );
        // Override is trusted even when no vendor detected (user knows
        // better — e.g. a fresh driver install the probe missed). Default
        // fallback when no vendor is CUDA (the more common path).
        assert_eq!(
            decide_gpu_mode(0.0, false, false, false, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cuda
        );
    }

    /// Explicit `--cpu-only` override forces Cpu mode regardless of VRAM.
    #[test]
    fn override_false_forces_cpu_with_huge_vram() {
        assert_eq!(
            decide_gpu_mode(24.0, true, false, false, Some(false), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// User `--gpu` on an Apple Silicon machine still gets Metal — they
    /// can't do CUDA on M-series silicon. We refuse to mis-classify.
    #[test]
    fn override_true_on_apple_silicon_still_metal() {
        assert_eq!(
            decide_gpu_mode(0.0, false, false, true, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Metal
        );
    }

    /// User `--cpu-only` on an Apple Silicon machine wins — they get CPU
    /// (Metal is opt-in via the vendor compose overlay; user-explicit
    /// override beats the auto pick).
    #[test]
    fn override_false_on_apple_silicon_yields_cpu() {
        assert_eq!(
            decide_gpu_mode(0.0, false, false, true, Some(false), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// Custom threshold — user passed `--gpu-vram-threshold-gb 4.0` to
    /// run on a smaller card with a smaller model stack.
    #[test]
    fn custom_threshold_lower_qualifies_smaller_cards() {
        assert_eq!(
            decide_gpu_mode(4.0, true, false, false, None, 4.0),
            GpuMode::Cuda
        );
        assert_eq!(
            decide_gpu_mode(3.9, true, false, false, None, 4.0),
            GpuMode::Cpu
        );
    }

    /// Custom threshold — user passed `--gpu-vram-threshold-gb 16.0` to
    /// be more conservative on a bigger model stack.
    #[test]
    fn custom_threshold_higher_rejects_8gb_card() {
        assert_eq!(
            decide_gpu_mode(8.0, true, false, false, None, 16.0),
            GpuMode::Cpu
        );
        assert_eq!(
            decide_gpu_mode(16.0, true, false, false, None, 16.0),
            GpuMode::Cuda
        );
    }

    /// Probe failed but vendor confirmed (vram=0.0, has_nvidia=true) —
    /// conservative fallback to CPU. Avoids enabling a GPU overlay against
    /// an unknown card that might OOM the model.
    #[test]
    fn vendor_known_but_vram_probe_failed_is_cpu() {
        assert_eq!(
            decide_gpu_mode(0.0, true, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// Serde round-trip — the FE consumes this enum via Tauri commands.
    /// Pin the wire shape so a future refactor of variant names is a
    /// review-able change. Includes v0.2.20 `Rocm` variant.
    #[test]
    fn gpu_mode_serializes_lowercase() {
        assert_eq!(serde_json::to_string(&GpuMode::Cuda).unwrap(), r#""cuda""#);
        assert_eq!(serde_json::to_string(&GpuMode::Rocm).unwrap(), r#""rocm""#);
        assert_eq!(serde_json::to_string(&GpuMode::Cpu).unwrap(), r#""cpu""#);
        assert_eq!(
            serde_json::to_string(&GpuMode::Metal).unwrap(),
            r#""metal""#
        );
    }

    // ─── v0.2.20: AMD / ROCm — NEW TESTS ──────────────────────────────

    /// AMD with sufficient VRAM, no override → ROCm.
    #[test]
    fn amd_with_8gb_returns_rocm() {
        assert_eq!(
            decide_gpu_mode(8.0, false, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Rocm
        );
        assert_eq!(
            decide_gpu_mode(16.0, false, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Rocm
        );
    }

    /// AMD below threshold → CPU (same rule as NVIDIA path).
    #[test]
    fn amd_with_3gb_returns_cpu() {
        assert_eq!(
            decide_gpu_mode(3.0, false, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// AMD with a low per-module threshold (v0.2.20 per-module threshold) →
    /// ROCm even when orchestrator-core's 8 GB default would degrade to CPU.
    /// Mirrors the RL reranker case (manifest declares 4 GB).
    #[test]
    fn amd_with_low_threshold_qualifies_for_small_module() {
        // 5 GB card, RL-style 4 GB threshold → ROCm.
        assert_eq!(
            decide_gpu_mode(5.0, false, true, false, None, 4.0),
            GpuMode::Rocm
        );
        // Same card, orchestrator-core's 8 GB threshold → CPU.
        assert_eq!(
            decide_gpu_mode(5.0, false, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// User `--gpu` on an AMD-only machine → ROCm (not CUDA fallback).
    /// Verifies the override path prefers the detected vendor.
    #[test]
    fn amd_user_override_true_returns_rocm() {
        assert_eq!(
            decide_gpu_mode(2.0, false, true, false, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Rocm
        );
    }

    /// User `--cpu-only` wins over AMD detection (same rule as NVIDIA).
    #[test]
    fn amd_user_override_false_returns_cpu() {
        assert_eq!(
            decide_gpu_mode(16.0, false, true, false, Some(false), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// NVIDIA + AMD both present (rare workstation case): NVIDIA wins.
    /// CUDA tooling is more mature than ROCm, so the conservative default
    /// is the better-supported stack.
    #[test]
    fn nvidia_preferred_when_both_present() {
        // Auto mode.
        assert_eq!(
            decide_gpu_mode(16.0, true, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cuda
        );
        // User-override=true path also picks NVIDIA when both detected.
        assert_eq!(
            decide_gpu_mode(16.0, true, true, false, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cuda
        );
    }

    /// User `--gpu` on Apple Silicon: Metal still wins (NOT ROCm/CUDA).
    /// Apple has neither, so the override must route to Metal — anything
    /// else would mis-classify the machine.
    #[test]
    fn apple_silicon_user_override_true_returns_metal_not_rocm() {
        // Plain Apple Silicon — no NVIDIA/AMD.
        assert_eq!(
            decide_gpu_mode(0.0, false, false, true, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Metal
        );
        // Defensive: phantom AMD flag (shouldn't happen — Apple Silicon
        // can't drive AMD discrete cards). Apple Silicon still wins.
        assert_eq!(
            decide_gpu_mode(0.0, false, true, true, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Metal
        );
    }

    /// AMD below threshold but user_override=true still forces ROCm.
    /// Mirrors the NVIDIA `override_true_forces_gpu_below_threshold` test.
    #[test]
    fn amd_user_override_true_below_threshold_still_rocm() {
        assert_eq!(
            decide_gpu_mode(2.0, false, true, false, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Rocm
        );
    }
}
