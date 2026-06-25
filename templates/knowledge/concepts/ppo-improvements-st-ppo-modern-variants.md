---
title: PPO Improvements - SORL and Modern Variants
type: concept
tags: [AI, reinforcement-learning, policy-optimization, LLM-training, multi-turn-reasoning, low-level-implementation]
created: 2026-02-27T00:00:00Z
updated: 2026-06-25T00:00:00Z
valid_from: 2026-02-27T00:00:00Z
valid_until: null
status: active
---

## Overview

Recent work identifies and fixes instability in PPO when training multi-turn LLM agents. SORL (Stabilizing Off-policy Reinforcement Learning) combines turn-level importance sampling with clipping-triggered normalization to prevent training collapse on complex reasoning tasks. Its two instantiations are SO-PPO (keeps a critic) and SO-GRPO (critic-free).

## The Problem: Why Vanilla PPO Fails on Multi-Turn Tasks

### Root Cause 1: Granularity Mismatch

**Issue**: PPO optimizes at token level, but multi-turn tasks have structure:
```
Turn 1 (reason): "Let me analyze..."     [20 tokens]
Turn 2 (search): "I'll search for..."   [10 tokens]
Turn 3 (reason): "Based on results..."  [15 tokens]
```

**Problem**: Each token gets its own importance weight w_t, but logically they should share credit at turn level. Token-level noise amplifies variance.

### Root Cause 2: Off-Policy Critic Errors

**Issue**: Multi-turn tasks have:
- Delayed sparse rewards (only at end)
- Critic trained on old policy
- Off-policy samples critic hasn't seen
- High-variance advantage estimates

**Result**: Unreliable advantage estimates → extreme gradients → collapse

## SORL: Solution via Two Mechanisms

### Mechanism 1: Turn-Level Importance Sampling

**Standard PPO** (token-level):
```
w_t = π_new(y_t | x, y_{<t}) / π_old(y_t | x, y_{<t})
L = min(w_t * Â_t, clip(w_t, 1-ε, 1+ε) * Â_t)
```

**Turn-level** (aggregated at turn level):
```
Turn = (y_start_t, ..., y_end_t)

w_turn = (π_new(y_turn | x, y_{<turn}) / π_old(y_turn | x, y_{<turn}))^(1/|turn|)
       = exp(1/|turn| * Σ log(π_new(y_t)/π_old(y_t)))

L = min(w_turn * Â_t, clip(w_turn, 1-ε, 1+ε) * Â_t)
```

**Key insight**: Geometric mean of per-token ratios, normalized by turn length → stable credit assignment at turn granularity.

**Mathematical foundation** (Lemma 4.1):
```
∇ L_turn = E[1/|y| * Σ_k w_k^turn(θ) * Â^k/|y^k| * ∇ log π(y^k|x,y^<k)]
                          ^^^turn-level credit^^^
```

All tokens in same turn share aggregated advantage Â^k → lower variance.

### Mechanism 2: Clipping-Triggered Normalization

**Issue with naive clipping**: Clipping suppresses large updates but introduces bias:

```
L_PPO = min(w_t * A_t, clip(w_t, 1-ε, 1+ε) * A_t)
```

When clipping is active (w_t extreme), gradients are zeroed → discards valuable signal.

**Decompose PPO gradient** (Lemma 4.2):
```
∇ L = [Off-policy term]
    + [Advantage estimation error]
    - [Clipping bias term]  ← grows exponentially with training!
```

**Clipping-triggered normalization**:
```
C(θ) = E[1/|y| * Σ_t 𝟙{t ∉ β_token} * w_t * Â_t]
              indicator for clipped tokens
```

Directly normalize this term by downweighting highly off-policy samples:

```
∇ L_corrected = ∇ L_PPO - α * C(θ)
                reweight outlier samples
```

**Result**: Conservative gradient updates, avoids extreme spikes (Fig 3b shows gradient norm 10× more stable).

## SORL Algorithm

**Combine both mechanisms**:

```
Turn-Level Importance Sampling:    Align with task structure
Clipping-Triggered Normalization:  Downweight unreliable samples
                           ↓
   Stabilizing Off-policy RL (SORL → SO-PPO / SO-GRPO)
```

**Variants**:
1. **Turn-level only**: turn-level sampling without normalization (partial fix)
2. **Token-level + normalization**: clipping-triggered normalization on token-level PPO (helps but not enough)
3. **SO-PPO / SO-GRPO**: both mechanisms combined (best stability)

## Experimental Results

**Multi-turn Search Tasks** (Qwen2.5 models):

| Metric | Token-level PPO | Turn-level only | SO-PPO |
|--------|-----------|----------|--------|
| **Success Rate** | 20% (collapse) | 65% | 85% |
| **Gradient Norm** | Extreme spikes | Stable | Very stable |
| **Clipping Ratio** | 0.8 (high) | 0.5 | 0.3 (conservative) |

**Performance on Benchmarks**:
- General QA: SO-PPO +15% over baseline
- Multi-hop QA: SO-PPO +12%
- Medical Multiple-Choice: SO-PPO +18%

**Model Scales**:
- Qwen2.5-1.5B: SO-PPO 75% → token-level PPO 20% (3.75× improvement)
- Qwen2.5-7B: SO-PPO 82% → token-level PPO collapse (unstable)

## Related PPO Variants (2024-2025)

### TOPR (Tapered Off-Policy REINFORCE)
- Tapered importance sampling → reduce variance in off-policy setting
- Focus on positive/negative examples asymmetrically
- Better for trajectory-level credit assignment

### LUFFY (Learning to Uncorrupt Flawed Feedback)
- Handle incorrect reward signals
- Learn to ignore/correct bad trajectory labels
- Improve from noisy demonstration data

### GSPO (Group Sampling Policy Optimization)
- Inspired by GRPO (group relative)
- Apply group-level variance reduction to PPO
- Sequence-level importance ratios + token-level clipping

## Hyperparameters for SO-PPO

```yaml
# Turn-Level Sampling
turn_boundaries: auto  # Use <eot> tokens or loss mask

# Clipping
clip_ratio: 0.2
clipping_norm_weight: 1.0  # α in clipping-triggered normalization

# KL Regularization
use_kl_loss: true
kl_loss_coef: 0.001

# Advantage Estimation (GAE)
gae_lambda: 0.95
gamma: 0.99

# Optimization
learning_rate: 5e-5  # For 7B models
batch_size: 32
mini_batch_size: 8
num_update_epochs: 3
max_grad_norm: 1.0

# Critic
critic_learning_rate: 1e-4
value_loss_coef: 0.5
```

## When to Use SORL vs Alternatives

| Scenario | Best Choice | Why |
|----------|-----------|-----|
| **Token-level reward** (dense) | Standard PPO | SORL overhead not needed |
| **Multi-turn reasoning** | SO-PPO | Designed for this |
| **Mathematical solving** | GRPO | No critic overhead |
| **Code generation** | GRPO or SO-GRPO | Both work well |
| **Language modeling** | SO-PPO | Sequence structure matters |
| **Offline RL** | DPO or CQL | Not for RL fine-tuning |

## Code Availability

- **Paper**: Li et al., "Stabilizing Off-Policy Training for Long-Horizon LLM Agent via Turn-Level Importance Sampling and Clipping-Triggered Normalization" (https://arxiv.org/abs/2511.20718)
- **Framework**: verl, TRL (HuggingFace Transformers), OpenVLA
- **Community**: Implementations in TRL library (preferred for LLMs)

## Future Directions

1. **Adaptive turn-level detection**: Auto-identify task structure instead of manual <eot>
2. **Hierarchical credit**: Multi-level (token → turn → trajectory)
3. **Mixture of experts**: Different importance weighting per task type
4. **Energy-efficient**: Reduce off-policy samples via better sampling

## Comparison: Evolution of Policy Gradient Methods

```
REINFORCE (1992)
    ↓
Policy Gradient (2000s)
    ↓
A3C / A2C (2016)
    ↓
PPO (2017) ← Most used for LLMs until 2023
    ↓
GRPO (2024) ← No critic, memory-efficient
    ↓
SORL: SO-PPO / SO-GRPO ← Stable off-policy training for multi-turn LLM agents
```

[[implements::Proximal Policy Optimization]]
[[improves::token-level-variance]]
[[improves::off-policy-stability]]
[[relatedTo::GRPO - Group Relative Policy Optimization]]
[[relatedTo::MBPO - Model-Based Policy Optimization]]
