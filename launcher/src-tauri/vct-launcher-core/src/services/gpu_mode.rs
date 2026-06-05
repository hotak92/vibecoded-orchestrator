// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Copyright (C) VibeCoded Tools — licensed under AGPL-3.0-or-later.
//
//! `GpuMode` enum — the typed GPU-mode signal threaded through install,
//! start, and resume flows.
//!
//! v0.2.47: relocated from `launcher/src-tauri/src/commands/gpu_policy.rs`
//! into `vct-launcher-core` so the hub-side supervisor can resolve GPU
//! variants the same way the launcher-side installer does. The full
//! detection / decision policy (`decide_gpu_mode` + the persisted
//! `HardwareSnapshot`) stays in the launcher crate; only the typed
//! signal moves here.
//!
//! See [[file::knowledge/concepts/supervisor-image-resolution-variant-gap-2026-06-04.md]]
//! for the bug that drove the relocation (supervisor was substituting
//! the bare manifest.version instead of the variant-resolved tag, so
//! private GHCR images 401'd on the start path).

use serde::{Deserialize, Serialize};

/// The mode the GPU-using services (Ollama, code_embed, paid modules)
/// will run in.
///
/// **Wire shape**: serialized as lowercase string (`"cuda"`, `"rocm"`,
/// `"cpu"`, `"metal"`). The frontend consumes this via Tauri commands
/// + the persisted hardware snapshot. Renaming a variant is a wire-
/// breaking change — bump the snapshot version if it ever happens.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GpuMode {
    /// Discrete NVIDIA GPU with sufficient VRAM. Services use the
    /// NVIDIA compose overlay (`docker-compose.gpu.yml` — CDI or
    /// `--gpus all`).
    Cuda,
    /// Discrete AMD GPU with sufficient VRAM (v0.2.20). Services use
    /// the ROCm compose overlay (`docker-compose.rocm.yml` —
    /// `/dev/kfd` + `/dev/dri` device passthrough). Paid modules with
    /// `gpu_image_variants` map this to a `-rocm` image tag.
    Rocm,
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
