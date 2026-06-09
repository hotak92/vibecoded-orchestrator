---
title: Process Reward Models
type: concept
tags: [ai, llm, alignment, reasoning, math, reward-model, rlhf, verification, mid-level-architecture]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:46Z
status: active
---

# Process Reward Models

## Overview

Process Reward Models (PRMs) are reward models trained to evaluate the correctness of each
intermediate reasoning step in a multi-step solution, rather than only the final answer. They
provide step-level supervision signals, enabling detection and correction of errors mid-reasoning.

Contrasted with Outcome Reward Models (ORMs), which assign a single score to the complete
solution (pass/fail on final answer). PRMs are more informative but require richer training data.

## Key Papers

### Let's Verify Step by Step (Lightman et al., OpenAI, May 2023)
- arXiv: 2305.20050
- First large-scale demonstration that process supervision significantly outperforms outcome
  supervision for LLMs on mathematical reasoning
- Trained PRMs using human step-level labels on MATH benchmark problems
- Released **PRM800K**: 800,000 step-level correctness labels on LLM-generated solutions
  (available at github.com/openai/prm800k)
- Best PRM-guided search solved 78.2% of MATH problems vs ~56% with ORM-guided search
- Used Best-of-N (BoN) evaluation: generate N solutions, select highest-scored one

### The Lessons of Developing PRMs (Zhang et al., Qwen team, Jan 2025)
- arXiv: 2501.07301, 278 citations
- Identifies key pitfalls in PRM training and evaluation methodology
- Finding: Monte Carlo (MC) estimation-based data synthesis (common approach) yields inferior
  PRMs vs LLM-as-a-judge or human annotation
- MC estimation asks a completion model to finish the solution from each intermediate step; the
  completion success rate estimates step correctness. Problem: completion models evaluate final
  answer correctness, not step logical validity.
- Identifies bias in BoN evaluation: PRMs trained with BoN objective drift toward outcome
  assessment, losing process-level discrimination
- Introduces consensus filtering: combine MC estimation with LLM-as-a-judge for better labels

## PRMs vs ORMs

| Property | ORM | PRM |
|---|---|---|
| Supervision signal | Final answer only | Per-step labels |
| Training data cost | Low (check answer) | High (human/model per step) |
| Error localization | No | Yes — identifies which step fails |
| Reward sparsity | Sparse (end of rollout) | Dense (every step) |
| RL training signal | Noisy (correct answer from wrong reasoning) | Clean |
| Robustness | Vulnerable to spurious shortcuts | More robust |

### The "Correct Answer, Wrong Reasoning" Problem
ORMs can be fooled by solutions that reach the correct answer via incorrect intermediate steps
(e.g., algebraic errors that cancel out). PRMs penalize such solutions at the step level,
providing a stronger training signal for genuine mathematical competence.

## How PRMs Are Trained

### Human Annotation (Gold Standard)
Human raters label each reasoning step as: Positive / Negative / Neutral.
Cost: ~$1-5 per solution × many steps × many solutions. Only feasible for focused datasets
like PRM800K (MATH benchmark, 800K steps).

### Monte Carlo Estimation (Common Approximation)
For each intermediate step prefix, run N completions using a policy model. Estimate step
correctness as the fraction of completions that reach the correct final answer:
`P(step correct) ≈ correct_completions / N`
Problem: This conflates "this step is a productive intermediate point" with "the right answer
is reachable from here," which can diverge (e.g., a wrong step that happens to lead to the
right answer via luck).

### LLM-as-a-Judge
Use a strong LLM to critique each reasoning step. More reliable than MC estimation for logical
step validity, but inherits biases of the judge model.

### Consensus Filtering (Zhang et al., 2025)
Combine MC estimation and LLM-as-a-judge: only keep step labels where both agree. Sacrifices
data volume for label quality, improving final PRM performance.

## Applications

### Best-of-N (BoN) Search
Generate N reasoning chains; score each with PRM; select the highest-scoring complete solution.
Effective inference-time scaling: performance scales with N up to a point. PRM BoN outperforms
ORM BoN significantly (78.2% vs ~56% on MATH in Lightman et al.).

### Beam Search with PRM
Use PRM scores to prune search beam at each step — discard low-scoring partial solutions early.
More efficient than BoN for a given compute budget.

### RLVR (RL with Verifiable Rewards)
Train policy models with PRM as the dense reward signal in RL (PPO, GRPO). Step-level rewards
provide cleaner gradient signal than terminal-only rewards.

### Step-Level Error Detection
Directly use PRM to identify which step in a generated solution is incorrect, enabling targeted
correction without regenerating the full solution.

## Connection to Reasoning Models

PRMs are central to the training of "thinking" or "reasoning" models (o1, DeepSeek-R1, QwQ).
These models generate long chains of thought; PRMs provide step-level supervision that:
- Rewards valid reasoning steps even when final answer is wrong
- Penalizes logical errors even when they accidentally lead to correct answers
- Enable efficient search over reasoning trajectories at inference time

## Current Limitations

- Training data acquisition remains expensive and domain-specific
- MC estimation (cheap) is inferior; human labels (good) don't scale
- PRM scores can degrade at very long reasoning chains (distribution shift)
- BoN evaluation inflates PRM scores when policy generates "right answer, wrong process" outputs
- Cross-domain generalization: PRMs trained on MATH don't transfer well to code or science

## Open Resources

- **PRM800K** (OpenAI): 800K human step labels on MATH problems
- **Math-Shepherd** (Wang et al., 2024): Automated process labels via MC estimation
- Various Qwen and DeepSeek team PRMs released on HuggingFace

Links: [[relatedTo::Constitutional AI]], [[relatedTo::RLHF]], [[relatedTo::LLM Alignment]], [[relatedTo::Chain-of-Thought Reasoning]], [[relatedTo::Mathematical Reasoning LLMs]]
