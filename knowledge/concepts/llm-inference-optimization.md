---
title: LLM Inference Optimization
type: concept
tags: [ai, llm, inference, optimization, serving, deployment, latency, throughput]
created: 2026-03-30T00:00:00Z
updated: 2026-04-05T14:33:32Z
status: active
---

# LLM Inference Optimization

## Overview

LLM inference optimization encompasses techniques to reduce latency, memory consumption, and cost when serving large language models. The core pillars are: attention kernel optimization ([[relatedTo::FlashAttention]]), KV cache management ([[relatedTo::KV Cache Compression]]), speculative decoding ([[relatedTo::Speculative Decoding]]), model compression (quantization, pruning, distillation), and serving infrastructure (continuous batching, tensor/pipeline parallelism, disaggregated prefill/decode).

These techniques compose synergistically — a production deployment might combine FP8 KV cache quantization, PagedAttention, FlashAttention-3 kernels, and speculative decoding simultaneously.

## Production Baseline (2025-2026)

The recommended production stack:
1. **PagedAttention** + prefix caching (lossless, 2-4× throughput)
2. **FP8 KV cache quantization** (near-lossless memory reduction)
3. **Continuous batching** (maximize GPU utilization)
4. **FlashAttention-3** kernels on H100 (75% theoretical FLOPs)
5. **Speculative decoding** for latency-sensitive applications (3-6× speedup at low batch)

## Attention Kernel Optimization

### FlashAttention Evolution
- **FA-2**: Broadly portable, 50-73% A100 throughput
- **FA-3**: Hopper-specific (H100), 75% peak FLOPs via warp-specialized async execution, interleaved matmul+softmax, incoherent FP8 processing (2.6× less quantization error)
- **FA-4**: Targeting Blackwell GPUs with 5-stage warp-specialized pipeline

### FlashInfer
Extends FlashAttention with block-sparse KV cache formats, JIT kernel compilation, and load-balanced scheduling. Reduces inter-token latency by 29-69% and long-context latency by 28-30%. Emerging standard for serving frameworks. Tradeoff: JIT compilation overhead vs hardware-specific kernel performance.

## Speculative Decoding (2025 State)

### EAGLE-3 (NeurIPS 2025)
Achieves 3.0-6.5× speedup over autoregressive generation using a lightweight prediction head with 'training-time testing' that simulates inference conditions during training and fuses multi-layer information. Improves 20-40% over EAGLE-2 (which introduced context-dependent dynamic draft trees).

### Medusa
Parallel prediction heads (K heads for K-token lookahead) without modifying the base model. Simpler integration but lower speedups than EAGLE-3.

### Lookahead Reasoning
Step-level parallelism for chain-of-thought: proposes and verifies batches of future reasoning steps. Boosts speedup from 1.4× to 2.1× on math benchmarks (GSM8K, AIME). Particularly relevant for reasoning models (o1/o3-style).

## Serving Infrastructure

### Continuous Batching
Dynamic batching allows variable-length inputs without padding waste. Pioneered by Orca (2022), now standard in vLLM, TGI, TensorRT-LLM, SGLang.

### Disaggregated Prefill/Decode
Separates compute-bound prefill from memory-bound decode onto different hardware:
- **llm-d** (May 2025): Kubernetes-native distributed serving with independent stage scaling
- **Mooncake**: Similar disaggregated architecture for production
- **Tradeoff**: Better hardware utilization vs added network overhead and operational complexity

### Chunked Prefill
Separates long prompt processing from token generation, preventing prefill from blocking decode batches. Critical for maintaining latency SLAs under mixed workloads.

### PagedAttention (vLLM)
Emulates OS virtual memory paging for KV cache. Slashes waste to under 4%, enabling 2-4× throughput. Non-contiguous storage allows efficient memory sharing for parallel sampling and beam search.

## Model Compression

### Quantization (2025 Standard: W4A4KV4)
Industry standard is INT4 for weights, activations, and KV cache:
- **GPTQ, AWQ**: Post-training weight quantization
- **FP8**: Hopper-native, near-lossless for weights and KV cache
- **INT4/FP4**: Aggressive compression with measurable but manageable accuracy loss
- **GEAR**: 3-4× KV cache reduction via aggressive quantization + low-rank residual patches for outlier tokens

### Advanced KV Cache Compression (2025 Frontier)
- **RocketKV**: Two-stage KV cache compression for long-context LLMs — combines coarse pruning with fine-grained retention
- **Expected Attention**: Estimates attention from future query distributions for more principled cache eviction — moves beyond heuristic-based eviction (H2O, StreamingLLM)
- **Entropy-guided caching**: Selectively retains tokens based on information-theoretic measures

### Pruning and Distillation
Structured pruning removes attention heads or layers. Knowledge distillation compresses large models into smaller ones. Often combined with quantization.

## Cost Considerations

Production multi-agent system costs can escalate from $127/week to $47,000/month without optimization. Key mitigations: vLLM with PagedAttention (Stripe achieved 73% cost reduction), prefix caching, circuit breakers, cost controls, right-sizing model to task complexity.

## Contrasting Views

| Debate | Position A | Position B |
|--------|-----------|-----------|
| Lossless vs lossy KV cache | PagedAttention/prefix caching (no accuracy impact) | Quantization/eviction (higher compression, accuracy risk) |
| Monolithic vs disaggregated | Simpler ops, lower latency | Better hardware utilization at scale |
| Kernel specialization vs generality | FA-3/4 hardware-specific (max perf) | FlashInfer JIT (portable, compilation cost) |
| Speculative decoding at scale | Essential for latency SLAs | Unjustified complexity for throughput serving |

## Open Questions

- Can KV cache compression maintain accuracy at 1M+ tokens with FP4/INT4?
- Will disaggregated prefill/decode become default for multi-agent serving?
- How does speculative decoding interact with reasoning models (o1/o3-style long CoT)?
- Optimal composition of techniques for multi-agent orchestrators balancing latency, throughput, and cost?

[[relatedTo::FlashAttention]]
[[relatedTo::KV Cache Compression]]
[[relatedTo::Speculative Decoding]]
[[relatedTo::Mixture of Experts]]
[[relatedTo::Transformer Architecture]]
[[relatedTo::Efficient Inference 2025 - Speculative Decoding + Quantization Integration]]
[[relatedTo::PagedAttention and vLLM Serving]]
[[relatedTo::Neural Network Scaling Laws]]
