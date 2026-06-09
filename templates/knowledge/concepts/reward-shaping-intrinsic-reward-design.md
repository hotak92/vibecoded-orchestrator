---
title: Reward Shaping & Intrinsic Reward Design in RL
type: concept
tags: [AI, reinforcement-learning, reward-engineering, intrinsic-motivation, learning-guidance, mid-level-architecture]
created: 2026-02-27T00:00:00Z
updated: 2026-04-05T14:33:49Z
valid_from: 2026-02-27T00:00:00Z
valid_until: null
status: active
---

# Reward Shaping & Intrinsic Reward Design in RL

## Overview

Reward shaping enhances RL by modifying reward signals to accelerate learning without changing the optimal policy. Reward engineering designs the initial reward function. Together they address sparse rewards, long learning times, and misalignment with true objectives.

## Core Concepts

### Reward Engineering

**Definition**: Designing the initial reward function R(s,a,s') to reflect desired outcomes.

**Challenges**:
- Sparse rewards: No signal for most actions (Minecraft, robotics)
- Delayed rewards: Feedback comes much later (RL in games)
- Reward hacking: Agent exploits loopholes (wrong solution counts)
- Misalignment: Reward doesn't match designer's intent

**Example** (grid world navigation):
```
R(s,a,s') = { +10 if s' is goal
            {  -1 if s' is non-goal
```

### Reward Shaping

**Definition**: Modifying reward to improve learning speed without changing optimal policy.

**Key guarantee** (Potential-based shaping):
```
R'(s,a,s') = R(s,a,s') + γ*Φ(s') - Φ(s)

Where Φ(s) = potential function (heuristic estimate of state value)
```

**Proof**: Shaped reward only shifts value functions by constant Φ, so optimal policy unchanged.

## Potential-Based Reward Shaping

### Mathematical Framework

**Standard Bellman**:
```
V(s) = max_a [R(s,a) + γ * V(s')]
```

**Shaped Bellman**:
```
V'(s) = max_a [R'(s,a,s') + γ * V'(s')]
      = max_a [R(s,a,s') + (γ*Φ(s') - Φ(s)) + γ * V'(s')]
      = max_a [R(s,a,s') + γ * V'(s')] + (γ*Φ(s') - Φ(s))
                                      ^^^^^^^^^^^^^^^^^^^^^^^^
                                      shifts by constant
```

**Result**: Policy argmax unchanged; V'(s) = V(s) + Φ(s)

### Design Examples

**Maze-solving**:
```
Φ(s) = -manhattan_distance(s, goal)

Shaped reward:
R'(s,a,s') = +10 if goal else 0 + γ*Φ(s') - Φ(s)
           = +10 if goal else -distance_change

Agent gets incremental rewards for moving closer!
```

**Robotics (reaching)**: 
```
Φ(s) = -euclidean_distance(gripper, object)
       Encourages moving toward object

R' = sparse_task_reward + β*(-distance(new) + distance(old))
    = sparse_reward + β*progress_bonus
```

**Navigation**:
```
Φ(s) = -pathfinding_heuristic(s, goal)
       Expert knows optimal distance
       
R' = goal_reward + heuristic_guidance
```

## Intrinsic Reward Shaping

### Policy Gradient for Reward Design (PGRD)

**Meta-learning approach**: Optimize reward parameters online

```
θ* = argmax_θ lim_{N→∞} E[1/N * Σ_t R_O(s_t) | R(·,θ)]
               objective reward  agent-learned reward parametrized by θ
```

Update reward parameters via gradient ascent on observed objective return:
```
θ ← θ + α * ∇_θ (observed objective reward)
```

**Advantage**: Dynamically adjust reward shaping as agent learns.

### Learning Intrinsic Reward for Policy Gradient (LIRPG)

**Combine extrinsic + intrinsic rewards**:
```
G^{ex+in}(s_t, a_t) = G^{extrinsic}(s_t, a_t) + G^{intrinsic}(s_t, a_t)
                     = external_return + internal_progress

θ' ≈ θ + α * G^{ex+in} * ∇_θ log π(a_t|s_t)
```

**Intrinsic reward types**:
1. **Progress-based**: Progress toward sub-goals, milestones
2. **Prediction-error**: Curiosity (surprise at new states)
3. **Diversity-based**: Reward for diverse trajectories
4. **Skill-based**: Reward for learning new skills

**Results**: Reduced sample complexity, faster convergence on sparse-reward tasks.

## Common Pitfalls & Solutions

### Pitfall 1: Reward Sparsity

**Problem**: Agent gets reward only at episode end → no learning signal

**Solutions**:
```
1. Add intermediate rewards (sub-goals):
   R'(s,a,s') = checkpoint_bonus + goal_reward

2. Use potential shaping:
   R'(s,a,s') = R(s,a,s') + distance_decrease_bonus

3. Curiosity-driven bonus:
   r_intrinsic = ||s_{t+1} - predicted_s_{t+1}||²
```

### Pitfall 2: Reward Hacking

**Problem**: Agent finds unintended loopholes

**Example**: Reward for "boxes in goal" → agent stacks boxes in weird way, doesn't transport them
**Solution**: Multi-objective rewards
```
R'(s,a,s') = task_completion_bonus + efficiency_penalty + safety_penalty
           = goal_reward - steps_taken - collision_penalty
```

### Pitfall 3: Misaligned Rewards

**Problem**: Single scalar reward can't capture complex objectives

**Solution**: Vector-valued rewards
```
R_vec = [task_success, human_preference, efficiency, safety]

Multi-objective RL → Pareto frontier of policies
Agent learns tradeoffs explicitly
```

### Pitfall 4: Unintended Consequences

**Problem**: Reward incentivizes unwanted behavior in complex environment

**Solution**: Iterative validation + domain expertise
```
1. Design reward
2. Train agent
3. Observe behavior
4. If unexpected, adjust reward shaping
5. Repeat
```

## Scalar vs Vector Rewards

### Scalar Rewards
```
R(s,a,s') → single value
```
**Pros**: Simple, computationally efficient
**Cons**: Can't represent multi-objective nature of real tasks

### Vector Rewards
```
R(s,a,s') → [r_efficiency, r_safety, r_task, r_comfort]
```
**Pros**: Richer feedback, enables multi-objective learning
**Cons**: Requires preference learning (which objectives matter)

**Modern approach**: Use LLM to learn reward function from feedback
```
LLM(trajectory, human_feedback) → R_learned

Update via:
R'(s,a,s') = R_learned + β * R_shaping
```

## State-of-the-Art Techniques (2023-2025)

### Reward Learning from Trajectories

Use neural network to learn reward from demonstrations:
```
R_learned(s,a,s') = f_θ(s, a, s')

Train f via:
L = MSE(f_θ(demo_traj) - high_score,
        f_θ(random_traj) - low_score)
```

### Adaptive Shaping Coefficients

Dynamically adjust β(t) = weight of shaped reward:
```
β(t) = β_0 * decay(t)  # Start high, decay over time

Or:
β(t) = adaptive(convergence_rate, uncertainty)
       If learning plateaus → increase β
```

### Language-Guided Reward Shaping

Leverage pretrained LLM:
```
R'(s,a,s') = R(s,a,s') + λ * LLM_reward(s, a, s')

LLM_reward = semantic_relevance(action, goal)
           + natural_language_preference
```

## Practical Configuration

```yaml
# Basic Reward Shaping
reward_shaping_enabled: true
potential_function: distance_heuristic  # or learned

# Shaping Parameters
shaping_scale: 0.1  # β: weight of shaped reward
discount_factor: 0.99  # γ in Φ(s') - Φ(s)

# Adaptive Shaping
adaptive_shaping: true
initial_weight: 0.5
weight_decay: 0.995  # Exponential decay

# Intrinsic Motivation
intrinsic_reward_type: progress  # or curiosity, diversity
intrinsic_scale: 0.01

# Multi-Objective
use_vector_rewards: false  # or true for complex tasks
objective_weights: [1.0, 0.5, 0.2]  # task, efficiency, safety
```

## Benchmarks & Use Cases

**Simple Navigation (Sparse Reward)**:
- Without shaping: 1000 episodes to learn
- With potential shaping: 100 episodes (10× speedup)

**Robotics (Manipulation)**:
- Dense reward (hand-crafted): Sample-efficient, requires expertise
- Sparse + potential shaping: Easier to design, comparable efficiency
- Learned reward: Most general, requires preference data

**Game Playing**:
- Dense reward (points): Easy but can game the system
- Sparse + curiosity: Discover strategies without explicit design

## Code & Resources

- **TensorFlow/PyTorch**: Standard RL frameworks (stable-baselines3)
- **Reward Learning**: PREF-RL, Bradley-Terry model for preferences
- **LLM Rewards**: LLM-as-judge frameworks
- **Frameworks**: RLlib, Tianshou (both support reward shaping)

## Related RL Concepts

[[uses::Potential-Based Shaping]]
[[uses::Intrinsic Motivation Design]]
[[implements::Learning Guidance]]
[[relatedTo::Intrinsic Motivation & Curiosity-Driven Exploration in RL]]
[[relatedTo::Exploration Exploitation Tradeoff]]
[[relatedTo::Actor-Critic]]
[[relatedTo::Imitation Learning]]
