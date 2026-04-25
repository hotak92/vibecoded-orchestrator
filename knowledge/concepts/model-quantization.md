---
title: Model Quantization
type: concept
tags: [AI, quantization, inference, VRAM, optimization, PTQ, QAT, precision]
created: 2026-03-31T12:00:00Z
updated: 2026-04-05T14:33:36Z
status: active
---

# Model Quantization

Hub node for model quantization — reducing numerical precision of neural network weights and activations to decrease memory footprint, accelerate inference, and lower power consumption. See [[Model Quantization Techniques]] for method-specific details (GPTQ, AWQ, GGUF, bitsandbytes) and [[Model Quantization Strategies]] for decision frameworks.

## Core Concept

Quantization maps high-precision values (FP32/BF16) to lower-precision representations (INT8, INT4, INT2). Three elements can be quantized in transformers:

1. **Model weights** — static, no calibration needed. Primary target for inference optimization.
2. **Activations** — dynamic, require calibration data. Harder to quantize accurately.
3. **KV cache** — critical for long-context inference in decoder-only LLMs where cache memory dominates total consumption.

**Memory rule of thumb**: model GB ≈ (params in B) × (bits / 8). A 7B model at FP16 = ~14 GB; at INT4 = ~3.5 GB (inference-only weight footprint, excluding KV cache, optimizer states, or adapters).

## Two Primary Approaches

### Post-Training Quantization (PTQ)
Applied after training; quick deployment (5–30 minutes for 7B model). Lower accuracy retention than QAT.

- **GPTQ**: Layer-by-layer reconstruction error minimization. 90% quality retention. ~712 tok/s on H200.
- **AWQ**: Activation-aware, preserves top 1% important weights. 95% quality retention. ~741 tok/s on H200.
- **GGUF**: llama.cpp-native format, CPU/GPU flexible. Q4_K_M variant is best 4-bit quality/size trade-off. 92% quality. ~93 tok/s (CPU-optimized).
- **bitsandbytes**: Runtime NF4/INT8 quantization, ideal for QLoRA training. 95%+ quality. ~168 tok/s.

### Quantization-Aware Training (QAT)
Integrates quantization effects during training via simulated quantization (fake quant) in forward pass and straight-through estimator in backward pass. Recovers up to 96% of accuracy degradation and 68% of perplexity degradation vs PTQ, but requires 2–4 hours compute for 7B models.

**QAT limitations (circa 2025–2026)**: Excessive resource consumption, gradient propagation difficulties at ultra-low bit levels, and accuracy degradation at INT2/INT3 question whether training overhead is always justified.

## Granularity Levels

| Level | Description | Accuracy | Overhead |
|---|---|---|---|
| Per-tensor | One scale/zero-point per tensor | Lowest | Lowest |
| Per-channel | One per output channel | Better | Low |
| Per-group/block | One per N weights (e.g., group-128) | Best | Highest |

Mixed-precision strategies optimize per-component: weights use per-channel, activations use per-token/per-group, attention layers kept at higher precision while feed-forward layers aggressively compressed.

## Quantization + Complementary Techniques

- **Quantization + Gradient Checkpointing**: Reduces memory 50–80%. Quantization handles model storage; checkpointing trades compute for activation memory during training.
- **Quantization + Pruning**: Prune first, then quantize for higher compression ratios.
- **Quantization + Distillation**: Hybrid pipelines for deployment-optimized models.
- **Quantization + LoRA (QLoRA)**: 4-bit base weights + BF16 adapters. Standard recipe for fine-tuning 70B+ on consumer hardware.

> **Note on VRAM discrepancy**: Model Quantization Techniques reports 7B at INT4 needs ~3.5 GB (inference-only weight footprint). QLoRA reports 7B QLoRA needs ~8 GB. These are not contradictory — QLoRA includes optimizer states, adapter parameters, and activation memory on top of quantized base weights.

## Hardware Dependency

Performance varies dramatically by hardware:
- **NVIDIA Ada (RTX 4080 Super)**: AWQ/GPTQ best via Tensor Cores. FP8 NOT available on consumer cards.
- **CPU/Apple Silicon**: GGUF (llama.cpp) excels; ~93 tok/s vs 700+ on GPU.
- **Datacenter (H200/A100)**: Full Transformer Engine FP8 support, NVLink for sharding.

See [[RTX 4080 Super]] for 16 GB VRAM-specific constraints and benchmarks.

## Multi-Agent Relevance

Quantization is critical for deploying multiple agents simultaneously on resource-constrained environments. With 16 GB VRAM shared across an orchestrator's models (e.g., VLM + LLM + embedding model), aggressive INT4 quantization enables multi-model VRAM allocation that would be impossible at FP16.

## Open Questions

- Optimal quantization + gradient checkpointing ratio for different architectures
- Dynamic mixed-precision adaptation during inference based on memory pressure
- Long-term accuracy degradation patterns when combining ultra-low precision (1–4 bit) with continuous fine-tuning
- How do quantization methods perform in multi-agent reinforcement learning systems where multiple quantized models must coordinate in real-time?

## Links

[[relatedTo::Model Quantization Techniques]]
[[relatedTo::Model Quantization Strategies]]
[[relatedTo::QLoRA]]
[[relatedTo::Gradient Checkpointing]]
[[relatedTo::VRAM Management Pattern]]
[[relatedTo::VRAM Optimization]]
[[relatedTo::RTX 4080 Super]]

## Sources

- NVIDIA quantization guide: https://developer.nvidia.com/blog/model-quantization-concepts-methods-and-why-it-matters/
- Method comparison 2026: https://blog.premai.io/llm-quantization-guide-gguf-vs-awq-vs-gptq-vs-bitsandbytes-compared-2026/
- QAT vs PTQ analysis: https://medium.com/better-ml/quantization-aware-training-qat-vs-post-training-quantization-ptq-cd3244f43d9a
- IBM QAT overview: https://www.ibm.com/think/topics/quantization-aware-training
- GPU memory management: https://www.runpod.io/articles/guides/gpu-memory-management-for-large-language-models-optimization-strategies-for-production-deployment
- Advanced quantization survey: https://arxiv.org/pdf/2501.11847
- Quantization landscape 2024: https://arxiv.org/html/2411.02530v1
- ACM quantization review: https://dl.acm.org/doi/10.1145/3623402
