---
title: FlashAttention
type: concept
tags: [ai, attention, transformer, gpu, memory-efficiency, inference, training, optimization, low-level-implementation]
created: 2026-02-26T00:00:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# FlashAttention

## Overview

FlashAttention is an IO-aware exact attention algorithm that dramatically reduces the memory
footprint and improves speed of attention computation in transformers. Instead of materializing the
full N×N attention matrix in GPU high-bandwidth memory (HBM), it computes attention in tiles that
fit in fast on-chip SRAM. The result is mathematically identical to standard attention — it is not
an approximation.

Introduced by Tri Dao, Daniel Y. Fu, Stefano Ermon, Atri Rudra, and Christopher Ré at Stanford
(NeurIPS 2022). The insight is that GPU compute is faster than GPU memory bandwidth — naive
attention is memory-bandwidth-bound, not compute-bound.

Key results (v1): 2-4x end-to-end training speedup, 5-10x memory reduction, enables 10x longer
sequences at the same memory budget.

## The Core Problem

Standard attention requires materializing intermediate matrices:
1. Compute S = QK^T (N×N matrix) — written to HBM
2. Apply softmax: P = softmax(S) — read from HBM, computed, written back
3. Compute O = PV — read from HBM

For sequence length N, HBM reads/writes scale as O(N²). On modern GPUs, HBM bandwidth (~2 TB/s
on A100) is far slower than SRAM throughput (~20 TB/s). The ratio is ~10x, so memory-bound
operations severely underutilize the GPU's arithmetic units.

For a sequence of 8K tokens: the attention matrix alone is 8000×8000 × 4 bytes = 256MB. At 16K
tokens, it exceeds 1GB — prohibitive for long-context applications.

## The Solution: Tiling + Online Softmax

FlashAttention avoids materializing the full attention matrix by processing blocks:

1. **Tiling**: Divide Q, K, V into blocks that fit in SRAM.
2. **Online softmax**: Track running max and normalization factor incrementally as blocks are
   processed. This allows computing the correct softmax without seeing all scores at once.
3. **Recomputation in backward pass**: Rather than saving the full attention matrix for the
   backward pass (which would require O(N²) memory), FlashAttention recomputes attention from
   the saved Q, K, V blocks on the fly. Trading compute for memory.

**IO complexity**:
- Standard attention: O(N²) HBM reads/writes
- FlashAttention: O(N² d / M) HBM reads/writes, where d is head dimension, M is SRAM size
- For typical M >> d, this is a significant constant-factor reduction

**Memory complexity**: O(N) instead of O(N²) — only activations proportional to sequence length
are stored, not the full attention matrix.

## Version History

### FlashAttention v1 (Dao et al., NeurIPS 2022)
- Tiling + online softmax + backward recomputation
- 2-4x training speedup, 5-10x memory reduction
- Memory: O(N) vs O(N²)
- Enabled 4x longer sequences at same memory budget

### FlashAttention v2 (Dao, 2023, ICLR 2024)
- Better work partitioning across GPU thread blocks and warps
- Reduced non-matmul FLOPs (non-matmul ops are ~4x slower per FLOP on modern GPUs)
- Parallelization across sequence length dimension (enables multi-head parallelism)
- ~2x speedup over FA1; reaches ~50-73% of theoretical peak A100 throughput

### FlashAttention v3 (Dao, 2024)
- Targets Hopper (H100) GPU architecture specifically
- **Warp-specialized asynchronous execution**: Overlaps Tensor Core compute with TMA (Tensor Memory Accelerator) data movement using producer/consumer warp groups
- **Interleaved block-wise matmul and softmax**: Pipelines the two operations instead of serializing them, hiding softmax latency behind matmul compute
- **Incoherent processing for FP8**: Applies random orthogonal rotations to Q/K before FP8 quantization, reducing quantization error by 2.6× while maintaining FP8 speed
- Uses WGMMA (Warp Group Matrix Multiply-Accumulate) instructions
- Achieves 75-85% of theoretical max FLOPs (up from 35% with FA2 on H100)
- 740-840 TFLOPs on H100 with FP16; 1.2-1.3 PFLOPs with FP8
- 1.5-2x speedup over FA2 on H100

### FlashAttention v4 (Dao-AILab, 2026)
- Targets Blackwell data-center GPUs (SM100/SM103: B200, B300 Blackwell Ultra)
- **Redesigned async pipeline with warp specialization**: orchestration warps manage async loads while compute warps handle softmax in parallel
- **Software-emulated exponentials**: polynomial approximation on FMA units
- **Conditional softmax rescaling**: skips the rescale when the running max is unchanged
- Up to ~1,605 TFLOPs/s on B200 (~71% hardware utilization); ~2.7× over Triton implementations
- BF16-first; auto-selected on Blackwell by vLLM (v0.17.0+) and other frameworks

## FlashInfer: Production Extension

FlashInfer extends the FlashAttention paradigm for serving frameworks:
- **Block-sparse KV cache formats**: Supports non-contiguous, paged KV memory layouts (compatible with PagedAttention)
- **JIT kernel compilation**: Generates specialized kernels at runtime for specific configurations
- **Load-balanced scheduling**: Distributes work evenly across GPU SMs for irregular workloads
- Reduces inter-token latency by 29-69%, long-context latency by 28-30%
- Emerging standard for serving frameworks (vLLM, SGLang); NVIDIA ships TRT-LLM kernels through FlashInfer as of Feb 2026
- Won Best Paper at MLSys 2025 — kernel-level performance convergence means framework differentiation now comes from scheduling and caching, not kernel speed

**Tradeoff vs FlashAttention**: FlashInfer's JIT approach offers hardware generality but at the cost of compilation overhead. FA-3/FA-4 are hardware-specific but achieve peak performance.

## Key Techniques

**Shared memory tiling**: Load Q, K, V tiles into SRAM, compute partial attention, accumulate
into output — never write intermediate results to HBM.

**Online softmax algorithm (Milakov & Gimelshein, 2018)**: Maintain running max m and
normalization sum l. When seeing new block with max m', update: l' = e^(m-m') * l + new_sum.
This enables single-pass numerically stable softmax without materializing the full score vector.

**Causal masking**: Applied within tiles, no cost for upper-triangular masking.

**Memory coalescing**: Access patterns are structured to maximize HBM bandwidth utilization.

## Impact

- Adopted universally in modern LLM training (GPT-4, Llama, Mistral, etc.)
- Enabled long-context models: without FA, training on 8K+ tokens was impractical
- Integrated in PyTorch as `F.scaled_dot_product_attention()` (uses FA if available)
- Used in virtually all major inference frameworks (vLLM, TGI, TensorRT-LLM)
- Inspired a wave of IO-aware algorithm design in ML

## Tradeoffs and Limitations

- Kernel must be reimplemented per GPU architecture (CUDA SRAM size, warp configuration varies)
- FA3 is H100-specific, FA4 is B200-specific; FA2 remains the broadly portable version
- Slightly higher FLOP count than naive attention (due to recomputation in backward pass)
- Not beneficial for very short sequences (overhead of tiling logic dominates)
- Fragmentation problem: hardware-specific kernels vs portable libraries remains unresolved

## Links

[[relatedTo::Transformer Architecture]]
[[relatedTo::Speculative Decoding]]
[[relatedTo::KV Cache Compression]]
[[relatedTo::Mixture of Experts]]
[[relatedTo::LLM Inference Optimization]]
[[relatedTo::PagedAttention and vLLM Serving]]
[[relatedTo::Efficient Attention Mechanisms Survey 2025-2026]]
