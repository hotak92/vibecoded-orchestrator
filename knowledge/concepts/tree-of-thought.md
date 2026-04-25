---
title: Tree-of-Thought
type: concept
tags: [AI, prompting, reasoning, LLM, planning, search, NeurIPS-2023]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:34:01Z
status: active
---

# Tree-of-Thought

## Overview

Tree of Thoughts (ToT) is a prompting framework introduced by Yao et al. (NeurIPS 2023) that extends chain-of-thought (CoT) prompting by allowing language models to explore multiple reasoning paths simultaneously and use deliberate search to solve complex problems.

Standard CoT forces a single left-to-right reasoning path. ToT frames problem solving as a tree search where each node is a "thought" — a coherent text passage representing an intermediate reasoning step. The model generates, evaluates, and searches through multiple thoughts to find the best solution path.

## Core Concepts

### Thought Decomposition
Problems are decomposed into intermediate steps where each step is a natural language "thought." The granularity depends on the task:
- Math: one equation per thought
- Code: one function per thought
- Creative writing: one paragraph per thought

### Thought Generator
At each tree node, the model generates candidate next thoughts using two strategies:
- **Sample independently** — k thoughts sampled from same prompt (diversity)
- **Propose sequentially** — thoughts proposed one by one with prior context (coherence)

### State Evaluator
The model evaluates/scores each thought state using:
- **Value independently** — LLM scores each state (1–10 or pass/fail)
- **Vote across states** — LLM compares and selects best among candidates

### Search Algorithm
Tree traversal strategies:
- **BFS** (Breadth-First Search) — explore all nodes at level k before going deeper; good for small step counts
- **DFS** (Depth-First Search) — pursue promising paths deeper first; good for large trees
- **Beam search** — keep top-k paths at each step; balances quality/compute

## Algorithm

```
function TreeOfThoughts(problem, max_depth, breadth):
    root = initial_state(problem)
    beam = [root]

    for depth in range(max_depth):
        candidates = []
        for state in beam:
            thoughts = generate_thoughts(state, k=breadth)
            for thought in thoughts:
                new_state = apply_thought(state, thought)
                score = evaluate(new_state)
                candidates.append((new_state, score))

        beam = top_k(candidates, k=breadth)

    return best(beam)
```

## Results from Original Paper

On tasks requiring multi-step reasoning with backtracking:
- **Game of 24** (math): GPT-4 with CoT: 4% success. ToT: 74% success
- **Creative Writing**: ToT outputs rated significantly better by human evaluators
- **Mini Crosswords**: ToT solved 4× more puzzles than CoT

## Practical Implementation

```python
# Simple ToT using multiple LLM calls
def tree_of_thoughts(problem, model, depth=3, breadth=3):
    thoughts = [{"state": problem, "history": []}]

    for _ in range(depth):
        candidates = []
        for thought in thoughts:
            # Generate next steps
            for _ in range(breadth):
                next_thought = model.generate(
                    f"Problem: {problem}\nHistory: {thought['history']}\n"
                    f"Generate the next reasoning step:"
                )
                # Evaluate the step
                score = model.evaluate(
                    f"Rate the quality of this reasoning step (1-10): {next_thought}"
                )
                candidates.append({
                    "state": next_thought,
                    "history": thought["history"] + [next_thought],
                    "score": score
                })
        # Keep best breadth candidates
        thoughts = sorted(candidates, key=lambda x: x["score"])[-breadth:]

    return thoughts[-1]  # Return best final state
```

## When to Use ToT

**Good fit**:
- Mathematical reasoning with multiple approaches
- Planning problems requiring backtracking
- Tasks with clear quality metrics for intermediate steps
- Problems where exploring alternatives matters

**Not worth the cost**:
- Simple factual questions (standard prompting suffices)
- Creative tasks without clear quality criteria
- Time-sensitive applications (ToT is 10–100× more expensive)
- Tasks where all reasoning paths are equally valid

## Relationship to Other Techniques

| Technique | Description | Comparison |
|---|---|---|
| Chain-of-Thought | Single linear reasoning chain | ToT extends to tree |
| Self-Consistency | Multiple CoT paths, majority vote | ToT evaluates per-step, not just final |
| MCTS | Monte Carlo tree search | ToT uses LLM evaluation; MCTS uses rollouts |
| ReAct | Interleaved reasoning+action | ToT is reasoning only; ReAct includes tool use |
| RAG | Retrieval-augmented | Can combine: retrieve at each ToT node |

## Links

[[relatedTo::MCTS for LLM Planning]]
[[relatedTo::Claude 4.x Prompt Engineering]]
[[relatedTo::Self-Consistency Voting]]
[[relatedTo::CISC Voting - Confidence-Weighted Self-Consistency]]
[[relatedTo::Prompt Engineering for Code Generation]]
[[relatedTo::Agentic LLM Workflows]]
