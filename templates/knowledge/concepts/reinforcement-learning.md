---
title: Reinforcement Learning
type: concept
tags: [AI, RL, machine-learning, policy, reward, Q-learning, training]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:48Z
status: active
---

## Overview

Reinforcement Learning (RL) is a branch of machine learning where an **agent** learns to make decisions by interacting with an **environment** to maximize cumulative **reward**. Unlike supervised learning (learning from labeled examples) or unsupervised learning (finding patterns), RL learns from the consequences of actions.

The core loop: Agent observes state → selects action → environment transitions to new state → agent receives reward → repeat.

## Fundamental Concepts

### Key Components
- **Agent** — the learner/decision-maker
- **Environment** — everything the agent interacts with
- **State (s)** — current situation of the environment
- **Action (a)** — what the agent can do in a given state
- **Reward (r)** — scalar feedback signal from the environment
- **Policy (π)** — mapping from states to actions (what the agent learns)
- **Value function (V/Q)** — expected cumulative reward from a state (or state-action pair)
- **Episode** — one complete run from initial to terminal state

### Reward Signal
The reward signal encodes the goal. The agent maximizes expected **discounted cumulative reward**:
```
G_t = r_t + γ·r_{t+1} + γ²·r_{t+2} + ...
```
- **γ (gamma)** — discount factor (0–1); controls importance of future rewards
- γ close to 1: agent is patient, values future rewards
- γ close to 0: agent is myopic, prefers immediate rewards

### Exploration vs. Exploitation
- **Exploitation** — take the action known to give highest reward
- **Exploration** — try new actions to discover better rewards
- **ε-greedy** — with probability ε, explore randomly; otherwise exploit
- **UCB** (Upper Confidence Bound) — explore states with high uncertainty

## Algorithm Categories

### Value-Based Methods
Learn the value of state-action pairs; derive policy from values.

**Q-Learning** (off-policy, model-free):
```python
Q[s][a] += alpha * (reward + gamma * max(Q[s_next]) - Q[s][a])
```
- Tabular: works for small, discrete state/action spaces
- DQN (Deep Q-Network): neural network approximates Q function (used in Atari)
- **Double DQN** — reduces overestimation bias
- **Dueling DQN** — separates state value from advantage

### Policy Gradient Methods
Directly optimize the policy without computing values.

**REINFORCE**:
```python
loss = -log_prob(action) * reward  # Maximize actions that led to high reward
```
- **PPO** (Proximal Policy Optimization) — most widely used; clips policy updates for stability
- **TRPO** — trust region constraint; predecessor to PPO
- **A2C/A3C** — actor-critic with advantage function; reduces variance

### Actor-Critic Methods
Combine value estimation (critic) with policy optimization (actor):
- Critic estimates value function (reduces variance)
- Actor updates policy using critic's feedback

## RL in LLMs (RLHF)

Reinforcement Learning from Human Feedback (RLHF) is how modern LLMs (ChatGPT, Claude) are aligned:

1. **Supervised Fine-Tuning (SFT)** — train on human-written examples
2. **Reward Model Training** — train a model to predict human preferences
3. **PPO/GRPO Fine-Tuning** — use RL to optimize policy against reward model
4. **Constitutional AI** — use AI feedback instead of human feedback (Anthropic)

Recent work (DeepSeek R1, Qwen) uses simpler GRPO (Group Relative Policy Optimization) without a separate reward model.

## RL for Agent Routing

RL is used to optimize which AI agent/model handles a given query:
- **State**: query features, conversation history
- **Action**: select agent/model
- **Reward**: task success + cost penalty
- See: `Reward Shaping for Agent Routing - PAR and Bounded Q-Values`

## Key Challenges

- **Sample efficiency** — RL requires many interactions to learn
- **Sparse rewards** — delayed or infrequent reward signals are hard to learn from
- **Credit assignment** — which action in a long sequence caused the reward?
- **Stability** — RL training is notoriously unstable; sensitive to hyperparameters
- **Reward hacking** — agent finds unintended ways to maximize reward

## Related Links

[[relatedTo::Reward Shaping for Agent Routing - PAR and Bounded Q-Values]]
[[relatedTo::RL-Based Retrieval Reranking for Knowledge Graphs]]
[[relatedTo::Q-Learning]]
[[relatedTo::MCTS for LLM Planning]]
[[relatedTo::Fine-Tuning for Tool Calling]]
[[relatedTo::Agentic LLM Workflows]]
