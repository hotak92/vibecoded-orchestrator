---
title: Transformer Architecture
type: concept
tags: [ai, transformer, attention, deep-learning, architecture, neural-network, NLP, mid-level-architecture]
created: 2026-03-30T00:00:00Z
updated: 2026-04-05T14:34:01Z
status: active
---

# Transformer Architecture

## Overview

The Transformer is the foundational neural network architecture behind modern LLMs, introduced in "Attention Is All You Need" (Vaswani et al., 2017). It uses self-attention mechanisms to process sequences in parallel (unlike sequential RNNs), enabling models to capture long-range dependencies. The architecture consists of multi-head attention layers, feed-forward networks, positional encodings, and residual connections.

Modern LLMs are predominantly decoder-only transformers (GPT, Llama, Claude) with causal masking, while encoder-decoder structures (T5) remain used for seq2seq tasks and encoder-only models (BERT) for embeddings/classification.

## Self-Attention Mechanism

The core operation transforms input embeddings into Query (Q), Key (K), Value (V) matrices:

```
Attention(Q, K, V) = softmax((Q × K^T) / √d_k) × V
```

- **Scaling factor √d_k**: Prevents dot products from growing too large with high dimensionality, which would push softmax into extremely small gradient regions
- **Computational complexity**: O(n²) in sequence length for both time and memory
- **Multi-head attention**: Multiple parallel attention heads with different learned projections, concatenated and linearly projected. Allows attending to different representation subspaces simultaneously

## Positional Encoding

Attention is permutation-invariant — position must be injected explicitly:
- **Sinusoidal (original)**: Fixed frequency-based encodings
- **Learned positional embeddings**: Trainable per-position vectors
- **Rotary Positional Embeddings ([[relatedTo::Rotary Positional Embeddings (RoPE)]])**: Encodes position as rotation in d/2 subspaces — dot product depends only on relative distance, zero learnable params. De facto standard for Llama, Mistral, Qwen, DeepSeek. Extended to 2M+ tokens via YaRN
- **ALiBi**: Adds linear bias based on distance; simpler but RoPE wins on downstream quality

## Attention Variants for Efficiency

### Multi-Head Attention (MHA)
Standard: each of H heads has independent Q, K, V projections. KV cache scales as 2 × layers × H × d_head × seq_len.

### Multi-Query Attention (MQA)
All query heads share a single K and a single V head. Drastically reduces KV cache size (H× reduction) but can degrade quality.

### Grouped-Query Attention (GQA)
Interpolates between MHA and MQA: groups of query heads share K/V. Used by Llama 2 70B (8 groups), Mistral 7B. Balances quality (close to MHA) with inference speed (close to MQA).

### Multi-Head Latent Attention (MLA)
Used by DeepSeek-V2/V3. Compresses KV into a low-rank latent space, further reducing cache size.

## KV Cache in Autoregressive Generation

During generation, previously computed K and V tensors are cached to avoid redundant computation:

```
Memory = 2 × num_layers × hidden_size × seq_len × sizeof(dtype)
```

For Llama 2 7B at 4096 tokens (batch=1, FP16): ~2GB. Memory grows linearly with both batch size and sequence length, becoming the primary bottleneck for long-context inference. This motivates [[relatedTo::KV Cache Compression]] techniques.

## Architecture Variants

| Variant | Structure | Examples | Use Case |
|---------|-----------|----------|----------|
| Encoder-only | Bidirectional attention | BERT, RoBERTa | Classification, embeddings |
| Decoder-only | Causal masking | GPT, Llama, Claude | Language generation |
| Encoder-decoder | Cross-attention | T5, mBART | Translation, seq2seq |
| Hybrid Transformer+SSM | Attention + state space | Jamba, Zamba | Long-context efficiency |

## Scaling via Mixture of Experts

Dense FFN layers can be replaced with [[relatedTo::Mixture of Experts]] networks, where a router activates only top-K experts per token (typically K=2-8). This scales total parameters while keeping per-token compute constant. DeepSeek-V3 achieves ~3% activation ratio.

## Competing Architectures

**State Space Models (Mamba, Hyena)**: Achieve linear O(n) complexity and up to 100× faster inference at very long sequences, but fall short on copying tasks, in-context learning, and computer vision. Transformers maintain clear superiority in vision tasks. Hybrid architectures combining attention + SSM layers are emerging rather than pure replacement.

**Open debate**: "Is attention all you need?" — increasingly challenged by SSMs achieving competitive language results, analog in-memory attention (Nature 2025), and alternative architectures. Architecture choice may remain task-dependent rather than having a universal winner.

## Inference Optimization

Key optimizations for transformer inference: [[relatedTo::FlashAttention]] (IO-aware attention kernels), [[relatedTo::KV Cache Compression]] (memory reduction), [[relatedTo::Speculative Decoding]] (latency reduction). For the full serving stack, see [[relatedTo::LLM Inference Optimization]].

[[relatedTo::FlashAttention]]
[[relatedTo::KV Cache Compression]]
[[relatedTo::Speculative Decoding]]
[[relatedTo::Mixture of Experts]]
[[relatedTo::LLM Inference Optimization]]
[[relatedTo::Efficient Attention Mechanisms Survey 2025-2026]]
[[relatedTo::Rotary Positional Embeddings (RoPE)]]
[[relatedTo::Neural Network Scaling Laws]]
