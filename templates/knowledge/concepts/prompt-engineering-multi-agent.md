---
title: Prompt Engineering — Multi-Agent (2026)
type: concept
tags: [prompt-engineering, multi-agent, llm, coordination, orchestration, mid-level-architecture]
created: 2026-06-09T00:00:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# Prompt Engineering — Multi-Agent (2026)

Prompt patterns for systems where multiple LLM agents collaborate. Covers orchestrator/specialist topologies, agent-to-agent communication conventions, distributed planning loops, and the system-prompt "contract" structure that makes a specialist actually behave as one.

For foundational single-prompt patterns see [[Prompt Engineering — Fundamentals]]; for tool/resource prompts inside the Model Context Protocol see [[Prompt Engineering — MCP and Tool Design]].

## When to use multi-agent (and when not to)

**Use when**:
- The task can be parallelised (specialists work simultaneously)
- The domain is complex enough to need diverse expertise
- Iterative refinement is valuable (agents review each other's work)

**Don't use when**:
- Simple, linear tasks — a single agent is faster
- Steps are tightly coupled — chaining simpler than coordination overhead
- Real-time / low-latency requirements — multi-agent coordination has too much overhead

Empirically, throwing a multi-agent system at a problem that a single CoT prompt can solve almost always regresses quality and latency. The litmus test: can you describe the goal to one expert in 200 tokens? Then don't use multi-agent.

## Coordination topologies

### Orchestration (puppeteer pattern)

One coordinator agent decomposes and delegates; specialists report back.

```markdown
# Orchestrator Agent Prompt
You coordinate specialist agents to complete complex tasks.

**Available Agents**:
- CodeAnalyzer: Understands code structure, dependencies
- SecurityAuditor: Finds vulnerabilities
- PerformanceOptimizer: Identifies bottlenecks
- DocumentationWriter: Creates docs

**Your Role**:
1. Break down the user task into sub-tasks
2. Assign each sub-task to an appropriate specialist
3. Collect results from specialists
4. Synthesize the final answer
5. If agents disagree, mediate and decide

**Task Delegation Format**:
@AgentName: [specific task]
Context: [relevant info from prior agents]
Deadline: [if applicable]
```

### Agent-to-agent (A2A) communication

Specialists talk laterally with a shared protocol. Useful when one specialist's output naturally provokes another's expertise (e.g. caching → cache poisoning).

```markdown
# Specialist Agent Prompt
You are SecurityAuditor, part of a multi-agent system.

**Your Expertise**: Finding security vulnerabilities

**Communication Protocol**:
- Need info from another agent → @AgentName [question]
- Sharing findings → [FINDING] severity, location, description
- Uncertain → [QUESTION] @Orchestrator [clarification needed]
- Done → [COMPLETE] summary

**Collaboration Rules**:
- If CodeAnalyzer mentions authentication, review it for security
- If PerformanceOptimizer suggests caching, check for cache poisoning
- Share findings with all agents using [BROADCAST]

Example:
[FINDING] High severity: SQL injection in auth/login.py line 45
[BROADCAST] @all Authentication module needs review
@PerformanceOptimizer: Is caching user credentials? If yes, [CRITICAL ISSUE]
```

### Distributed planning (MARL-style)

Each agent carries its own goal/state/actions/observations and the protocol normalises how those are shared. Useful for long-running multi-objective work (e.g. balancing security vs performance vs maintainability).

```markdown
# Multi-Agent Reinforcement Learning Pattern

Each agent maintains:
- Goal: what I'm trying to achieve
- State: what I currently know
- Actions: what I can do
- Observations: what I've seen from other agents

Communication format (natural language):
[GOAL] My objective: Optimize database queries
[STATE] Current understanding: 15 queries identified, 3 are slow
[ACTION] Proposing: Add index on users.email column
[REQUEST-FEEDBACK] @all Does this conflict with your goals?
[OBSERVATION] @SecurityAuditor mentioned email used in auth — proceed carefully
```

## Prompt structure for a multi-agent run

The shared-context-then-agent-specific-prompts structure keeps each agent focused while ensuring everyone agrees on goal/constraints.

```markdown
# Shared Context (all agents see this)
**Project**: Authentication refactoring
**Goal**: Improve security while maintaining performance
**Constraints**: No breaking changes, must pass existing tests
**Deadline**: 2 hours

# Agent-Specific Prompts
## @SecurityAuditor
Your focus: Find vulnerabilities
Ignore: Performance (other agent handles that)
Report format: [Security finding template]

## @PerformanceOptimizer
Your focus: Reduce latency
Ignore: Security (other agent handles that)
Report format: [Performance finding template]

## @Orchestrator
Your role: Mediate if security vs performance tradeoffs arise
Decision criteria: Security > Performance (unless critical business need)
```

## System-prompt "contract" structure

A good system prompt reads like a **short contract** — explicit, bounded, verifiable. This template works for specialist agents and for top-level orchestrators.

```markdown
# System Prompt: [Agent Name]

## Role
You are [specific role with domain expertise].

## Goal
[Primary objective, measurable if possible]

## Constraints
- [Technical constraint 1]
- [Policy constraint 2]
- [Resource constraint 3]

## Uncertainty Handling
When unsure:
1. [First action: ask user, search docs, etc.]
2. [If still uncertain: suggest options]
3. [Never: guess, proceed without confirmation]

## Output Format
[Exact format specification]

Example:
[Good example of desired output]

## Tools Available
- [Tool 1]: Use when [scenario]
- [Tool 2]: Use when [scenario]

## Success Criteria
You succeed when:
- [Criterion 1]
- [Criterion 2]
```

### Why each section pays for itself

- **Role**: anchors the agent's reasoning style and vocabulary
- **Goal**: prevents drift into adjacent tasks
- **Constraints**: turns implicit assumptions into testable rules
- **Uncertainty handling**: stops "best-guess" failure modes
- **Output format**: makes downstream parsing reliable (essential when another agent consumes the output)
- **Tools available**: prevents hallucinated tool calls; documents when each is appropriate
- **Success criteria**: gives the agent something to self-verify against

## Model selection across the topology

The same project usually mixes models by cost/quality:

- **Premium tier** (Claude Opus 4.8, GPT-4-class): orchestrator that mediates trade-offs, code-reviewer that catches subtle bugs, architect that makes irreversible decisions. ~5% of agent calls but ~80% of decision weight.
- **Balanced tier** (Claude Sonnet 4.6): coder, planner, doc-writer. ~25% of calls.
- **Cheap tier** (Claude Haiku 4.5 or comparable): validators, formatters, summarisers, routing-classifiers. ~70% of calls.
- **Free tier** (local Ollama): preprocessing, deterministic transforms, low-stakes summaries.

The cost-to-quality dial is the orchestrator's prompt — by deciding "this sub-task goes to Haiku, this one to Opus", a thin orchestrator can deliver Opus-tier final quality at ~30% of Opus-tier cost.

## Anti-patterns

**Overloaded orchestrator**: orchestrator does the actual work instead of delegating. Symptom: orchestrator prompt is >2000 tokens and contains domain-specific details. Fix: delegate to a specialist.

**Echo chambers**: all specialists report to the orchestrator but never see each other's findings. Symptom: orchestrator re-derives the same conclusions multiple times. Fix: use A2A `[BROADCAST]` for cross-cutting findings.

**Implicit handoffs**: agent A's output doesn't match agent B's expected input format. Symptom: agent B asks for clarification or invents missing fields. Fix: define output schemas in each agent's system prompt and reference them in the orchestrator's delegation template.

**Specialist scope creep**: SecurityAuditor starts commenting on performance. Symptom: noisy findings outside the agent's lane. Fix: explicit `Ignore: [...]` lines in the agent-specific prompt.

**No tie-breaker**: two specialists disagree, orchestrator paralyses. Fix: pre-declare decision criteria in the orchestrator's system prompt (e.g. "Security > Performance unless critical business need").

## Failure-mode prompts

These are short stanzas to drop into a specialist's system prompt to harden it against common failure modes:

- **Refuse-unknown-tools**: "If a tool is not in your Tools Available list, refuse to call it and report `[MISSING-TOOL] <name>` instead of inventing arguments."
- **Bounded retries**: "If a tool returns an error, retry at most twice with refined arguments. After that, report `[TOOL-BLOCKED] <name> <error>` and yield."
- **Confidence thresholds**: "If your internal confidence is below 70%, respond with `[LOW-CONFIDENCE]` and ask the orchestrator to consult another specialist instead of producing a final answer."
- **Anti-hallucination citations**: "Every factual claim about the codebase must cite a file path + line range. Claims without citations are treated as hypotheses, not findings."

## Reading

- [WMAC 2026](https://multiagents.org/2026/) — LLM-Based Multi-Agent Collaboration
- [AAMAS 2026](https://cyprusconferences.org/aamas2026/) — Autonomous Agents and Multiagent Systems
- Anthropic's published multi-agent reports (use search for the latest version; the field moves fast).

[[relatedTo::Prompt Engineering — Fundamentals]]
[[relatedTo::Prompt Engineering — MCP and Tool Design]]
[[relatedTo::Agentic LLM Workflows]]
