---
title: Actor-Critic
type: concept
tags: [AI, reinforcement-learning, actor-critic, PPO, A2C, TD3, DDPG, policy-gradient, multi-agent]
created: 2026-03-30T00:00:00Z
updated: 2026-04-05T14:33:07Z
status: active
---

# Actor-Critic

## Overview

Actor-Critic methods simultaneously learn a policy (actor) and a value function (critic), combining policy gradient and value-based approaches. The critic evaluates actions using temporal difference learning to provide low-variance feedback for policy updates, enabling scalability to continuous action spaces.

## Core Mechanism

```
Actor π_θ(a|s): generates actions (policy gradient updates)
Critic V_φ(s) or Q_w(s,a): evaluates actions (TD learning)
TD error: δ = r + γV(s') - V(s)
Policy gradient: ∇_θ J ≈ E[δ · ∇_θ log π_θ(a|s)]
```

The advantage function A(s,a) = Q(s,a) - V(s) serves as baseline for variance reduction without introducing bias.

## Key Variants

### A3C / A2C (Mnih et al., 2016)
- **A3C**: Multiple parallel workers with asynchronous gradient updates
- **A2C** (synchronous): More stable gradients, better GPU utilization
- Uses advantage function for variance reduction
- Foundation for many modern methods

### PPO (Schulman et al., 2017)
Clipped surrogate objective preventing large policy updates:
```
L^CLIP = E[min(r(θ)·Â, clip(r(θ), 1-ε, 1+ε)·Â)]
where r(θ) = π_θ(a|s) / π_old(a|s)
```
Simplifies TRPO's KL-divergence constraint. De facto standard for on-policy RL and RLHF.

### SAC (Soft Actor-Critic)
Off-policy with entropy regularization and clipped double-Q:
- 25-40% higher sample efficiency than PPO in continuous control
- Stochastic policy with entropy bonus prevents premature convergence

### TD3 (Twin Delayed DDPG)
- Twin critics: min(Q1,Q2) reduces overestimation bias
- Delayed policy updates (update actor less frequently than critic)
- Target policy smoothing with added noise

### DDPG (Deep Deterministic Policy Gradient)
- First deep actor-critic for continuous control
- Off-policy with deterministic policy + OU noise exploration
- Superseded by TD3 and SAC due to stability issues

## On-Policy vs Off-Policy Tradeoff

| Property | On-Policy (PPO, A2C) | Off-Policy (SAC, TD3) |
|----------|---------------------|----------------------|
| Stability | More stable, easier to tune | Requires careful engineering |
| Sample efficiency | Lower (discard old data) | 25-40% higher (replay buffer) |
| Exploration | Stochastic policy | Entropy bonus or noise |
| Distribution shift | None | Risk from replay buffer |

## Single vs Twin Critics

- **Single critic**: simpler but suffers overestimation bias
- **Twin critics** (TD3, SAC): pessimistic value estimates via min(Q1,Q2), more stable

## Generalized Advantage Estimation (GAE)

Interpolates between high-bias (1-step TD) and high-variance (Monte Carlo):
```
Â^GAE(γ,λ) = Σ_{l=0}^∞ (γλ)^l · δ_{t+l}
```
λ=0 gives TD(0), λ=1 gives Monte Carlo. Typically λ=0.95.

## Connection to Intrinsic Motivation

ICM (Pathak et al. 2017) integrates with A3C by adding prediction error in learned feature space as intrinsic reward:
```
r_total = r_extrinsic + β · ||ŝ_{t+1} - s_{t+1}||²
```
Significantly improves exploration in sparse-reward environments.

**ICM vs RND debate**: ICM uses learned forward model (adaptive, more complex); RND uses fixed random target (simpler, cheaper, but less effective for long-horizon exploration).

## Multi-Agent Actor-Critic (CTDE)

Centralized Training with Decentralized Execution:
- **MADDPG**: Centralized critic Q(o₁,...,oₙ, a₁,...,aₙ) during training, decentralized actors
- **MAPPO**: Extends PPO to multi-agent with shared or independent critics
- **Value decomposition** (QMIX, VDN, COMA): Credit assignment in cooperative settings

**Scalability**: Fully centralized critics scale O(|A|^n); value decomposition is scalable but limited to monotonic value functions. 2025 debate: CTDE may not be "centralized enough" for complex coordination.

## Theoretical Advances

2025 result: Optimal O(dH⁵log|A|/ε²) sample complexity with strategic exploration, resolving open problem in actor-critic theory.

## Applications (2024-2025)

- Robotics navigation (MADAC: 94.54% success rate)
- Autonomous drones (Actor-Critic MPC hybrid)
- Energy management systems
- LLM alignment (PPO in RLHF pipeline)
- LLM-based voice-guided robot navigation

## Key References

- Konda & Tsitsiklis (2000): Actor-critic convergence proofs
- Mnih et al. (2016): A3C
- Schulman et al. (2017): PPO
- Haarnoja et al. (2018): SAC
- Fujimoto et al. (2018): TD3
- Lowe et al. (2017): MADDPG

[[relatedTo::SAC - Soft Actor-Critic with Entropy Regularization]]
[[relatedTo::GRPO - Group Relative Policy Optimization]]
[[relatedTo::RLHF]]
[[relatedTo::Intrinsic Motivation & Curiosity-Driven Exploration in RL]]
[[relatedTo::Exploration Exploitation Tradeoff]]
[[relatedTo::Imitation Learning]]
[[relatedTo::Centralized Training Decentralized Execution]]
