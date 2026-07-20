---
title: Agent Orchestration
type: concept
tags: [AI, agents, orchestration, LLM, workflow, coordination, agentic, mid-level-architecture]
created: 2026-02-26T00:00:00Z
updated: 2026-07-20T00:00:00Z
status: active
---

## Overview

Agent Orchestration refers to the process of managing, coordinating, and directing multiple AI agents to work together on complex tasks. An orchestrator takes a high-level goal, decomposes it into subtasks, assigns them to appropriate agents, monitors progress, handles failures, and assembles final results.

Orchestration differs from simple chaining in that it handles dynamic task decomposition, agent selection, retry logic, and parallel execution.

## Orchestration Responsibilities

1. **Task decomposition** — break complex goals into atomic, parallelizable subtasks
2. **Agent selection** — match task requirements to agent capabilities and model tier
3. **Resource management** — balance parallel work vs. context window limits
4. **Progress tracking** — monitor which tasks are complete, in-progress, blocked
5. **Error handling** — detect failures, retry, escalate, or skip
6. **Result assembly** — combine partial outputs into coherent final result
7. **Context injection** — provide each agent with exactly the context it needs

## Orchestration Patterns

### Static (Pre-defined DAG)
Task graph defined upfront; good for well-understood workflows:
```python
tasks = [
    Task("research", depends_on=[]),
    Task("code", depends_on=["research"]),
    Task("test", depends_on=["code"]),
    Task("deploy", depends_on=["test"])
]
```

### Dynamic (LLM-Driven)
Orchestrator LLM decides task decomposition at runtime:
```
Orchestrator: "Given this goal, what subtasks are needed?"
→ LLM returns task list
→ Orchestrator spawns agents
→ Repeat based on results
```
More flexible but less predictable; harder to control costs.

### Reactive (Event-Driven)
Tasks triggered by events (file change, API call, task completion):
- Post-file-edit hook → sync KG
- Commit → run tests → deploy
- Error → alert agent

## Orchestrator Models

| Tool/Framework | Type | Strengths |
|---|---|---|
| Claude Code Agent tool | Native | Simple, integrated with Claude |
| LangGraph | Graph-based | Stateful, complex workflows |
| Microsoft Agent Framework | Multi-agent | Microsoft, conversation-based |
| CrewAI | Role-based | Easy specialization |
| Claude-Flow | MAS framework | Claude-optimized patterns |

## Communication Protocols

**Tool-based** (modern, preferred):
```python
# Agent calls tool to communicate
result = await delegate_task(
    agent="coder",
    task="implement auth module",
    context={"spec": auth_spec, "files": existing_files}
)
```

**Message-based**:
```python
await send_message(
    recipient="coder",
    content="Please implement auth per the attached spec",
    summary="Auth implementation request"
)
```

**Shared state** (blackboard):
```python
blackboard.claim_task(task_id="auth-impl", agent="coder")
blackboard.update_task(task_id="auth-impl", status="completed", output=code)
```

## Handoff Format Best Practice

When delegating to sub-agents (300–500 tokens):
```
@agent-name (Model)
Task: [One sentence goal]
Context: [File paths, key patterns, constraints]
Success Criteria: [What "done" looks like]
Output: [Where to save results]
```

## Cost Optimization

- **Right-size models**: Use Haiku for simple tasks, Sonnet for implementation, Opus rarely
- **Limit parallelism**: 3 parallel agents max (context overflow risk in VS Code)
- **Focused context**: Each agent gets only what it needs (not the full history)
- **Early termination**: Stop agents when success criteria met, not when context exhausted
- **Caching**: Reuse agent outputs when inputs haven't changed

## Failure Handling

```python
async def orchestrate_with_retry(task, max_retries=3):
    for attempt in range(max_retries):
        try:
            result = await spawn_agent(task)
            if validate_result(result):
                return result
            # Retry with more specific guidance
            task.context += f"\nPrevious attempt failed: {result.error}"
        except AgentTimeout:
            task.priority += 1  # Escalate
    raise OrchestratorError(f"Task failed after {max_retries} attempts")
```

## This Project's Orchestration

The orchestrator uses:
- **Claude Code Agent tool** for ad-hoc agent spawning
- **Blackboard pattern** for multi-step task lists (CONTEXT_STATE.md)
- **MCP servers** as tools accessible to all agents
- **Hook system** for reactive automation (post-file-edit → KG sync)

Generic multi-agent orchestration patterns apply across many frameworks.

## Related Links

[[relatedTo::Multi-Agent Systems]]
[[relatedTo::Agentic LLM Workflows]]
[[relatedTo::Model Context Protocol]]
