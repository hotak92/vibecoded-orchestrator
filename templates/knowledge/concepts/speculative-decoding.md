---
title: Speculative Decoding
type: concept
tags: [ai, llm, inference, optimization, efficiency, decoding, EAGLE, low-level-implementation]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:53Z
status: active
---

# Speculative Decoding

## Overview

Speculative decoding is a lossless inference acceleration technique for autoregressive LLMs. It
uses a small, fast "draft" model to propose multiple tokens ahead, then validates them in parallel
with the larger target model in a single forward pass. Correct tokens are accepted; rejected tokens
cause early stopping with the target model sampling a replacement. Output distribution is provably
identical to standard autoregressive decoding.

Introduced concurrently by two independent papers in late 2022/early 2023:
- **Leviathan et al. (2022)** — "Fast Inference from Transformers via Speculative Decoding" (Google Research). Demonstrated 2-3x speedups on T5-XXL.
- **Chen et al. (2023)** — "Accelerating Large Language Model Decoding with Speculative Sampling" (DeepMind). Demonstrated 2-2.5x speedups on a 70B Chinchilla model.

## Core Mechanics

### Draft-Verify Loop
1. **Draft phase**: The small draft model generates k tokens autoregressively (typical k = 4-8).
2. **Verify phase**: The target model processes the full sequence (context + k draft tokens) in one
   forward pass using causal masking. This yields k+1 probability distributions simultaneously.
3. **Acceptance criterion**: Each drafted token x_i is accepted with probability:
   `min(1, p_target(x_i) / p_draft(x_i))`
   If rejected, sample from a residual distribution `max(0, p_target - p_draft)` to correct.
4. **Context update**: All accepted tokens are appended; the next draft cycle begins.

### Why It's Lossless
The acceptance/rejection scheme (speculative sampling) ensures the marginal distribution of each
output token matches the target model exactly. It is not an approximation — just a rescheduling
of the same computation.

### Why It Works Empirically
LLMs generate highly predictable tokens in structured or factual contexts. After "The capital of
France is," the draft model correctly proposes "Paris" with high confidence. Speedup correlates
with the average acceptance rate across tokens.

## Key Parameters and Tradeoffs

| Parameter | Effect |
|---|---|
| Draft model size | Smaller = faster drafting, lower acceptance rate |
| Speculation length k | Larger k = more tokens verified per pass, but more wasted work on rejection |
| Domain alignment | Better alignment between draft and target = higher acceptance rate |

**Speedup range**: 2-4x wall-clock time in practice, depending on acceptance rate and hardware.
Speedup is maximized on memory-bandwidth-bound hardware (single-batch inference) where the large
model is bottlenecked on loading weights, not arithmetic.

## Variants and Extensions

### EAGLE Family (State of the Art)
- **EAGLE (2024)**: Dedicated draft model trained end-to-end with the target model's hidden states
- **EAGLE-2 (2024)**: Introduces context-dependent dynamic draft trees that adapt speculation structure per-token
- **EAGLE-3 (NeurIPS 2025)**: Achieves **3.0-6.5× speedup** using a lightweight prediction head with "training-time testing" — simulates inference conditions during training and fuses multi-layer information. 20-40% improvement over EAGLE-2

### Medusa (2024)
Adds multiple parallel prediction heads (K heads for K-token lookahead) to the target model itself, eliminating the separate draft model. Does not modify the base model architecture.

### Other Approaches
- **Self-speculative decoding**: Uses early exit layers of the target model as the draft
- **Online Speculative Decoding (OSD, 2024)**: Continuously fine-tunes the draft model online using corrections from the target to improve acceptance rate over time
- **SPRINTER (2025)**: Approximate verification to further reduce verification cost
- **SpecInfer**: Combines multiple draft models for higher throughput in server settings

### Lookahead Reasoning (2025)
Adds step-level parallelism for chain-of-thought: proposes and verifies batches of future reasoning steps rather than individual tokens. Boosts speedup from 1.4× to 2.1× on math benchmarks (GSM8K, AIME). Particularly relevant for reasoning models (o1/o3-style).

## Interaction with Quantization

A key 2025 finding: speculative decoding and quantization do not naively combine well. On 4-bit quantized models, tree-style draft verification incurs MORE overhead than single-token forward pass because quantization shifts the bottleneck from memory to compute. Hierarchical frameworks that convert tree drafts to sequence drafts via an intermediate model achieve 2.78× speedup vs 1.31× for naive EAGLE-2 on quantized Llama-3-70B. See [[relatedTo::Efficient Inference 2025 - Speculative Decoding + Quantization Integration]].

### DeFT: Tree-Structured Attention for Speculation (ICLR 2025)
DeFT introduces tree-structured attention optimized for multi-candidate speculative decoding scenarios. By applying prefix-aware and load-balanced partitioning, DeFT reduces 73-99% of KV cache I/O during verification, achieving 2.23-3.59x speedup over standard verification approaches. This directly addresses the bottleneck when verifying large draft trees.

### SpecAttn (Feb 2026)
Co-designs sparse attention with self-speculative decoding within vLLM, using the target model's own shallow layers as the draft mechanism while applying sparse attention patterns during verification. Eliminates the need for a separate draft model while maintaining memory efficiency through PagedAttention.

## Practical Deployment

Supported natively in HuggingFace Transformers via `assistant_model` parameter in `generate()`.
Also implemented in vLLM, TensorRT-LLM, and most production inference stacks.

Best suited for:
- Single-stream (batch size 1) inference where memory bandwidth is the bottleneck
- Domains where draft model and target model have high token overlap
- Latency-sensitive applications (chatbots, coding assistants)

Less effective for:
- High-batch-size throughput-optimized serving (target model is compute-bound, benefits diminish)
- Creative/high-temperature generation (acceptance rate drops)

**Batch size limitation**: Benefits diminish or vanish at high batch sizes where the system is already compute-saturated. Debate exists on whether speculative decoding adds unjustified complexity for throughput-oriented batch serving, though latency SLAs may demand it regardless.

## Relationship to KV Cache

Speculative decoding reuses the KV cache for the shared context. The k draft tokens are verified
with a single prefill-style pass; accepted tokens are appended to the cache normally. Rejected
tokens require recomputing only the correction token.

## Links

[[relatedTo::KV Cache Compression]]
[[relatedTo::FlashAttention]]
[[relatedTo::Mixture of Experts]]
[[relatedTo::LLM Inference Optimization]]
[[relatedTo::Transformer Architecture]]
[[relatedTo::Efficient Inference 2025 - Speculative Decoding + Quantization Integration]]
[[relatedTo::PagedAttention and vLLM Serving]]
[[relatedTo::Efficient Attention Mechanisms Survey 2025-2026]]
