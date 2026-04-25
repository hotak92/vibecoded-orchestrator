---
title: Fine-Tuning Techniques for Open-Source LLMs
type: concept
tags: [fine-tuning, LoRA, QLoRA, PEFT, adapter-tuning, LLM, optimization, 2025]
created: 2026-02-27T00:00:00Z
updated: 2026-04-05T14:33:25Z
valid_from: 2025-01-01T00:00:00Z
valid_until: null
status: active
---

# Fine-Tuning Techniques for Open-Source LLMs

## Overview

Fine-tuning adapts pre-trained large language models to domain-specific tasks without full retraining. Modern techniques enable efficient fine-tuning on consumer hardware through parameter-efficient methods, making LLM customization accessible at scale.

## Core Problem & Motivation

### Full Fine-Tuning Costs (2025)

| Model Size | VRAM Needed | Hardware | Cost |
|------------|------------|----------|------|
| 7B | 40-50GB | 2×A100/H100 | $10K+ |
| 70B | 400GB | 8×H100 | $100K+ |
| 7B (Inference) | 14GB | 1×A100 | $1K |

**Question**: Can we fine-tune 70B models on consumer hardware?

**Answer**: Yes, via LoRA/QLoRA on RTX 4090 ($1.5K).

## Techniques

### 1. LoRA (Low-Rank Adaptation) — 2021

**Principle**: Instead of updating all weights, learn small "delta" matrices.

**Mathematics**:
```
Original weights: W (weight matrix)
LoRA update: W ← W + AB^T
Where: A is r×d, B is r×d (r = rank, typically 8-64)
Parameters: r × 2d << d × d (original)
```

**Example: 7B Model**:
- Original: 7B parameters
- LoRA rank-8: ~50M parameters (0.7% of original)
- Training memory: ~20GB (vs 40GB for full)

**Pros**:
- Minimal memory overhead
- Fast training
- Composable (combine multiple LoRAs for different tasks)

**Cons**:
- Quality slightly lower than full fine-tuning
- Rank selection empirically challenging
- Not all layers benefit equally

### 2. QLoRA (Quantized Low-Rank Adaptation) — 2023

**Innovation**: Combine LoRA with 4-bit quantization.

**Mechanism**:
```
1. Load base model in 4-bit (int4, 8 bits per 2 values)
2. Keep forward pass in 4-bit
3. Compute gradients in higher precision (bfloat16)
4. Update LoRA parameters (small, in bfloat16)
```

**Hardware Requirements**:
- 7B model: 6-8GB VRAM (vs 20GB for LoRA)
- 70B model: 48GB VRAM (vs 400GB for full)
- Deployment: RTX 4090 (consumer GPU)

**Trade-offs**:
- Slight quality reduction from quantization
- Training slower than LoRA (4-bit ops overhead)
- Inference same as base model (no LoRA overhead)

### 3. Other Parameter-Efficient Methods

#### Prefix Tuning
- Prepend learnable "prefix" embeddings to input
- Only prefix parameters trained
- Limitation: Less effective than LoRA on large models

#### Adapter Layers
- Small bottleneck layers inserted between model layers
- 0.5-2% additional parameters
- Better composability than LoRA
- Research-only (limited production adoption)

#### Prompt Tuning
- Optimize input prompts as soft vectors
- No model parameters changed
- Very data-efficient but weaker than tuning

#### Full Parameter Tuning
- Update all weights (traditional approach)
- Highest quality but expensive
- Only viable for well-resourced teams

## Practical Recommendations (2025)

### Choose Based on Context

| Scenario | Recommended | Reason |
|----------|-------------|--------|
| **Single GPU, <48GB** | QLoRA | Only viable option |
| **Full control, no budget constraint** | Full fine-tuning | Best quality |
| **Production composability** | LoRA or Adapters | Combine multiple models |
| **Data efficient (<1K examples)** | Prompt/Prefix tuning | Avoid overfitting |
| **Research/exploration** | QLoRA + LoRA comparison | Fast iteration |

### Hyperparameter Guidelines

```yaml
LoRA/QLoRA Configuration:
  rank: 8-64  # Empirical: 16 common sweet spot
  alpha: 16-32  # Learning scale
  target_modules: ["q_proj", "v_proj"]  # Attention keys
  dropout: 0.05  # Prevent overfitting
  learning_rate: 5e-4  # 5x higher than full tuning
  batch_size: 4-16  # Depends on VRAM
  num_epochs: 3-5  # Usually 1-3 epochs optimal
```

## Tools & Platforms (2025-2026)

### Open-Source

**Axolotl**
- Flexible YAML config system
- Supports: LoRA, QLoRA, full, instruction tuning
- Multi-GPU optimized
- Community: Strong, active

**LLaMA-Factory**
- Specialized for LLaMA/LLaMA2/LLaMA3
- Pre-configured LoRA/QLoRA recipes
- Easy CLI interface
- Multi-GPU: Optimized

### Commercial Platforms

**SiliconFlow** (2025-2026)
- Cloud-based fine-tuning
- 3-step pipeline: upload → configure → deploy
- H100/H200 backed
- Inference: 2.3× faster than competitors
- Privacy: No data retention

**Hugging Face AutoTrain**
- Web interface for fine-tuning
- Automatic hyperparameter tuning
- Free tier available
- Community model hub

**Firework AI**
- Optimized training pipelines
- Fast fine-tuning (claimed speedups)
- User-friendly interface
- Growing ecosystem

## Benchmarking Results (2025)

### QLoRA Effectiveness
- **7B model on RTX 4090**: ~2 hours for 5K examples
- **70B model on RTX 4090**: ~24-48 hours for 5K examples
- **Quality gap vs full tuning**: 2-5% (task dependent)

### LoRA vs QLoRA Trade-offs
```
Training Speed: LoRA > QLoRA (30% faster)
Memory Usage: QLoRA < LoRA (6x less for 70B)
Quality: LoRA ≈ QLoRA (within margin)
Inference: Both << full model
```

## Integration with Production

### Deployment Patterns

1. **Base + LoRA stack**
   - Deploy base model once
   - Load multiple LoRAs per task
   - Cost-efficient multi-tenant inference

2. **QLoRA for consumer apps**
   - Compress to GPTQ/AWQ post-training
   - Deploy on consumer GPUs
   - Edge deployments viable

3. **Continuous learning**
   - Fine-tune as new data arrives
   - Update LoRA without retraining base
   - No distribution shift impact

## Advanced Topics (2025)

### Multi-LoRA & Mixture
- Train multiple LoRAs for different domains
- Route tokens to appropriate LoRA
- Emerging technique, still research

### LoRA Merging
- Mathematically merge LoRA into base model
- Trade: Loss composability, gain speed
- Used pre-deployment for speed-critical apps

### Knowledge Distillation
- Compress fine-tuned 70B → 7B model
- Use QLoRA for efficient distillation
- Emerging pattern for production

## Connection to Broader ML Trends

### Relates to
- [[relatedTo::Mixture of Experts]]: MoE enables sparse activation; LoRA enables parameter efficiency
- [[relatedTo::Efficient Attention Mechanisms Survey 2025-2026]]: Both address computational bottlenecks differently
- [[relatedTo::Quantization Techniques for LLMs]]: Often combined (QLoRA)

### Enables
- **Democratization**: Custom models without massive budgets
- **Privacy**: Fine-tune on proprietary data locally
- **Agility**: Rapid task adaptation

## Key Takeaway

Fine-tuning techniques have matured in 2025-2026:
- **LoRA**: Production-standard for efficiency
- **QLoRA**: Makes 70B+ fine-tuning accessible
- **Ecosystem mature**: Multiple platforms, clear best practices
- **Quality parity**: LoRA/QLoRA within 2-5% of full tuning

For most organizations in 2025, QLoRA on consumer hardware is the default choice for custom model adaptation.

[[relatedTo::Mixture of Experts]]
[[relatedTo::Efficient Attention Mechanisms Survey 2025-2026]]
[[implements::Parameter-Efficient Training]]
[[uses::Low-Rank Factorization]]
[[uses::Quantization]]
