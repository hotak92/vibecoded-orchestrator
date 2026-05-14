//! GPU mode decision policy (v0.2.9 Bug K).
//!
//! Pure decision function that maps (vram, vendor, user_override) onto a
//! `GpuMode` enum. The same logic lives in `install.py::_decide_gpu_mode`
//! (Python side); the two implementations are kept in lock-step so the
//! launcher's `apply_hardware_reconfig` flow and the install-time decision
//! arrive at the same answer for the same inputs.
//!
//! ## Why a threshold?
//!
//! The default model stack is:
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
//! whole stack. The threshold is configurable (`--gpu-vram-threshold-gb`
//! on install.py) for users with non-default model picks; the Rust side
//! reads the same threshold from the install-manifest when present.
//!
//! ## Cross-platform
//!
//! Apple Silicon has unified memory, so a VRAM number doesn't apply
//! directly — `GpuMode::Metal` is its own arm. `has_apple_silicon: true`
//! always wins over the VRAM threshold (i.e. an Apple Silicon machine
//! always gets Metal regardless of the threshold).

use serde::{Deserialize, Serialize};

/// Default VRAM threshold (GiB) below which we degrade to CPU-only mode.
///
/// Tuned for the default model stack — see module docstring. Keep this
/// in sync with `install.py::_DEFAULT_GPU_VRAM_THRESHOLD_GB`.
pub const DEFAULT_GPU_VRAM_THRESHOLD_GB: f64 = 8.0;

/// The mode the GPU-using services (Ollama, code_embed) will run in.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GpuMode {
    /// Discrete NVIDIA/AMD GPU with sufficient VRAM. Services use the
    /// `gpu` compose overlay (CDI or `--gpus all`).
    Gpu,
    /// CPU-only. Either no discrete GPU, insufficient VRAM, or user
    /// override. Services fall back to CPU kernels (Ollama: CPU mode;
    /// code_embed: ONNX CPU runtime).
    Cpu,
    /// Apple Silicon — unified memory + Metal. Services use the
    /// Metal-aware code path (Ollama has built-in Metal acceleration,
    /// code_embed falls back to CPU because the sentence-transformers
    /// model lacks a Metal kernel today).
    Metal,
}

/// Pure decision function.
///
/// Precedence:
///   1. `user_override` (when explicit) wins over everything.
///   2. Apple Silicon → `Metal`. No threshold check — unified memory.
///   3. Discrete GPU vendor present AND VRAM ≥ threshold → `Gpu`.
///   4. Else → `Cpu`.
///
/// `vram_gb` of 0.0 with no vendor signals "no GPU detected" → `Cpu`.
/// `vram_gb` of 0.0 with a vendor signals "GPU detected but probe failed"
///   → `Cpu` (conservative — we'd rather not enable a GPU overlay against
///   an unknown card).
pub fn decide_gpu_mode(
    vram_gb: f64,
    has_nvidia: bool,
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
            // user_override=true + no Apple Silicon → trust them, return Gpu
            // even when VRAM is below threshold. The user has accepted the
            // tradeoff.
            return GpuMode::Gpu;
        }
        return GpuMode::Cpu;
    }

    if has_apple_silicon {
        return GpuMode::Metal;
    }

    if has_nvidia && vram_gb >= threshold_gb {
        return GpuMode::Gpu;
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
            decide_gpu_mode(4.0, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
        assert_eq!(
            decide_gpu_mode(7.99, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// Threshold is inclusive — 8.0 GB qualifies for GPU.
    #[test]
    fn vram_at_threshold_inclusive_enables_gpu() {
        assert_eq!(
            decide_gpu_mode(8.0, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Gpu
        );
    }

    /// Comfortable VRAM headroom — straightforward GPU enable.
    #[test]
    fn vram_well_above_threshold_enables_gpu() {
        assert_eq!(
            decide_gpu_mode(12.0, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Gpu
        );
        assert_eq!(
            decide_gpu_mode(24.0, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Gpu
        );
    }

    /// No NVIDIA + no Apple Silicon → CPU no matter the VRAM number.
    #[test]
    fn no_gpu_vendor_is_cpu() {
        assert_eq!(
            decide_gpu_mode(0.0, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
        // Bizarre but defensive: vram_gb set with no vendor (probe ambiguity)
        // — still CPU.
        assert_eq!(
            decide_gpu_mode(16.0, false, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// Apple Silicon always wins, regardless of VRAM number (unified memory
    /// — the `vram_gb` field doesn't apply directly).
    #[test]
    fn apple_silicon_returns_metal() {
        assert_eq!(
            decide_gpu_mode(0.0, false, true, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Metal
        );
        // Even with a phantom NVIDIA flag (shouldn't happen in practice
        // but defensively: Apple Silicon precedes vendor-discrete logic).
        assert_eq!(
            decide_gpu_mode(8.0, true, true, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Metal
        );
    }

    /// Explicit `--gpu` override forces Gpu mode regardless of VRAM, on
    /// non-Apple machines.
    #[test]
    fn override_true_forces_gpu_below_threshold() {
        assert_eq!(
            decide_gpu_mode(2.0, true, false, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Gpu
        );
        // Override is trusted even when no vendor detected (user knows
        // better — e.g. a fresh driver install the probe missed).
        assert_eq!(
            decide_gpu_mode(0.0, false, false, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Gpu
        );
    }

    /// Explicit `--cpu-only` override forces Cpu mode regardless of VRAM.
    #[test]
    fn override_false_forces_cpu_with_huge_vram() {
        assert_eq!(
            decide_gpu_mode(24.0, true, false, Some(false), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// User `--gpu` on an Apple Silicon machine still gets Metal — they
    /// can't do CUDA on M-series silicon. We refuse to mis-classify.
    #[test]
    fn override_true_on_apple_silicon_still_metal() {
        assert_eq!(
            decide_gpu_mode(0.0, false, true, Some(true), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Metal
        );
    }

    /// User `--cpu-only` on an Apple Silicon machine wins — they get CPU
    /// (Metal is opt-in via the vendor compose overlay; user-explicit
    /// override beats the auto pick).
    #[test]
    fn override_false_on_apple_silicon_yields_cpu() {
        assert_eq!(
            decide_gpu_mode(0.0, false, true, Some(false), DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// Custom threshold — user passed `--gpu-vram-threshold-gb 4.0` to
    /// run on a smaller card with a smaller model stack.
    #[test]
    fn custom_threshold_lower_qualifies_smaller_cards() {
        assert_eq!(
            decide_gpu_mode(4.0, true, false, None, 4.0),
            GpuMode::Gpu
        );
        assert_eq!(
            decide_gpu_mode(3.9, true, false, None, 4.0),
            GpuMode::Cpu
        );
    }

    /// Custom threshold — user passed `--gpu-vram-threshold-gb 16.0` to
    /// be more conservative on a bigger model stack.
    #[test]
    fn custom_threshold_higher_rejects_8gb_card() {
        assert_eq!(
            decide_gpu_mode(8.0, true, false, None, 16.0),
            GpuMode::Cpu
        );
        assert_eq!(
            decide_gpu_mode(16.0, true, false, None, 16.0),
            GpuMode::Gpu
        );
    }

    /// Probe failed but vendor confirmed (vram=0.0, has_nvidia=true) —
    /// conservative fallback to CPU. Avoids enabling a GPU overlay against
    /// an unknown card that might OOM the model.
    #[test]
    fn vendor_known_but_vram_probe_failed_is_cpu() {
        assert_eq!(
            decide_gpu_mode(0.0, true, false, None, DEFAULT_GPU_VRAM_THRESHOLD_GB),
            GpuMode::Cpu
        );
    }

    /// Serde round-trip — the FE consumes this enum via Tauri commands.
    /// Pin the wire shape so a future refactor of variant names is a
    /// review-able change.
    #[test]
    fn gpu_mode_serializes_lowercase() {
        assert_eq!(serde_json::to_string(&GpuMode::Gpu).unwrap(), r#""gpu""#);
        assert_eq!(serde_json::to_string(&GpuMode::Cpu).unwrap(), r#""cpu""#);
        assert_eq!(
            serde_json::to_string(&GpuMode::Metal).unwrap(),
            r#""metal""#
        );
    }
}
