---
title: PPO Improvements - ST-PPO and Modern Variants
type: concept
tags: [AI, reinforcement-learning, policy-optimization, LLM-training, multi-turn-reasoning]
created: 2026-02-27T00:00:00Z
updated: 2026-04-05T14:33:45Z
valid_from: 2026-02-27T00:00:00Z
valid_until: null
status: active
---

## Overview

Recent work (2024-2025) identifies and fixes instability in PPO when training multi-turn LLM agents. ST-PPO (Stabilized Turn-level PPO) combines turn-level importance sampling with clipping-bias correction to prevent training collapse on complex reasoning tasks.

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

## ST-PPO: Solution via Two Mechanisms

### Mechanism 1: Turn-Level Importance Sampling

**Standard PPO** (token-level):
```
w_t = π_new(y_t | x, y_{<t}) / π_old(y_t | x, y_{<t})
L = min(w_t * Â_t, clip(w_t, 1-ε, 1+ε) * Â_t)
```

**Turn-PPO** (aggregated at turn level):
```
Turn = (y_start_t, ..., y_end_t)

w_turn = (π_new(y_turn | x, y_{<turn}) / π_old(y_turn | x, y_{<turn}))^(1/|turn|)
       = exp(1/|turn| * Σ log(π_new(y_t)/π_old(y_t)))

L = min(w_turn * Â_t, clip(w_turn, 1-ε, 1+ε) * Â_t)
```

**Key insight**: Geometric mean of per-token ratios, normalized by turn length → stable credit assignment at turn granularity.

**Mathematical foundation** (Lemma 4.1):
```
∇ L_Turn-PPO = E[1/|y| * Σ_k w_k^turn(θ) * Â^k/|y^k| * ∇ log π(y^k|x,y^<k)]
                          ^^^turn-level credit^^^
```

All tokens in same turn share aggregated advantage Â^k → lower variance.

### Mechanism 2: Clipping-Bias Correction

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

**Clipping bias correction**:
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

## ST-PPO Algorithm

**Combine both mechanisms**:

```
Turn-Level Importance Sampling:  Align with task structure
Clipping-Bias Correction:        Downweight unreliable samples
                           ↓
         Stabilized Turn-level PPO (ST-PPO)
```

**Three variants**:
1. **Turn-PPO**: Only turn-level sampling (partial fix)
2. **S-PPO**: Clipping bias on token-level PPO (helps but not enough)
3. **ST-PPO**: Combined (best stability)

## Experimental Results

**Multi-turn Search Tasks** (Qwen2.5 models):

| Metric | Token-PPO | Turn-PPO | ST-PPO |
|--------|-----------|----------|--------|
| **Success Rate** | 20% (collapse) | 65% | 85% |
| **Gradient Norm** | Extreme spikes | Stable | Very stable |
| **Clipping Ratio** | 0.8 (high) | 0.5 | 0.3 (conservative) |

**Performance on Benchmarks**:
- General QA: ST-PPO +15% over baseline
- Multi-hop QA: ST-PPO +12%
- Medical Multiple-Choice: ST-PPO +18%

**Model Scales**:
- Qwen2.5-1.5B: ST-PPO 75% → Token-PPO 20% (3.75× improvement)
- Qwen2.5-7B: ST-PPO 82% → Token-PPO collapse (unstable)

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

## Hyperparameters for ST-PPO

```yaml
# Turn-Level Sampling
turn_boundaries: auto  # Use <eot> tokens or loss mask

# Clipping
clip_ratio: 0.2
clipping_bias_weight: 1.0  # α in gradient correction

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

## When to Use ST-PPO vs Alternatives

| Scenario | Best Choice | Why |
|----------|-----------|-----|
| **Token-level reward** (dense) | Standard PPO | ST-PPO overhead not needed |
| **Multi-turn reasoning** | ST-PPO | Designed for this |
| **Mathematical solving** | GRPO | No critic overhead |
| **Code generation** | GRPO or ST-PPO | Both work well |
| **Language modeling** | ST-PPO | Sequence structure matters |
| **Offline RL** | DPO or CQL | Not for RL fine-tuning |

## Code Availability

- **Paper**: Li et al. 2025 (ST-PPO, https://arxiv.org/abs/2511.20718)
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
ST-PPO (2025) ← Stable for multi-turn, keeps critic
```

[[implements::Proximal Policy Optimization]]
[[improves::token-level-variance]]
[[improves::off-policy-stability]]
[[relatedTo::GRPO - Group Relative Policy Optimization]]
[[relatedTo::MBPO - Model-Based Policy Optimization]]
