---
name: task-breakdown
description: Break complex features into implementable tasks with estimates, dependencies, and risk assessment
short_desc: decompose feature into tasks, estimates, dependencies
keywords: [task decomposition, effort estimation, dependency mapping, sprint planning, risk assessment, "implementation plan", "break into tasks", "task list for", "estimate this", "effort estimate", "decompose this"]
model: sonnet
---

# Task Breakdown (Sonnet)

**Purpose**: Break complex features into implementable tasks with estimates, dependencies, and risk assessment.

**Model**: Sonnet (balanced reasoning for task decomposition and estimation)

## When to Invoke Autonomously

Use this skill when:
1. **Complex Feature**: "Break down [feature] into tasks"
2. **Unclear Scope**: "Estimate effort for [ambiguous requirement]"
3. **Sprint Planning**: "Decompose epic into sprint tasks"
4. **Risk Assessment**: "Identify risks and blockers for [feature]"
5. **Dependency Mapping**: "What are dependencies for [feature]?"

## DO NOT invoke for

- Simple, obvious tasks (just implement)
- Already-broken-down work
- Single-step tasks

## Decision Tree

```
Feature work:
├─ Complex, needs breakdown? → Use this skill
├─ Unclear how much effort? → Use this skill
├─ Simple, obvious implementation? → Don't use this skill
└─ Already broken down? → Don't use this skill
```

## Usage

```
/task-breakdown break-down [feature description]
/task-breakdown estimate [feature with unknowns]
/task-breakdown dependencies [feature]
/task-breakdown risks [feature]
```

## What This Skill Does

**Task Decomposition**:
- Break features into SMART tasks (Specific, Measurable, Achievable, Relevant, Time-bound)
- Granularity: 1-2 hour tasks (sweet spot for tracking and estimation)
- Clear done criteria for each task

**Effort Estimation**:
- Three-point estimate (Optimistic, Most Likely, Pessimistic)
- Planning Poker for team estimation
- Historical data calibration
- Adjust for complexity, unknowns, team experience, dependencies

**Dependency Mapping**:
- Identify sequential vs parallel work
- Find critical path (longest dependency chain)
- Surface external blockers early
- Enable parallelization opportunities

**Risk Assessment**:
- Technical risks (new tech, complex algorithms, integration challenges)
- Dependency risks (external blockers, team availability, third-party services)
- Scope risks (unclear requirements, scope creep, hidden complexity)
- Mitigation strategies (prototyping, incremental delivery, parallel work, fallbacks)

**See**: `examples/estimation-methods.md` for estimation techniques, `examples/dependency-patterns.md` for dependency visualization, `examples/risk-matrix.md` for risk assessment framework

## Quick Workflow Reference

**Before planning**: Search for task breakdown and estimation patterns
```bash
.claude/scripts/kg-search search "planning" --type concept
```

**For deep research**: Ask user "Use hybrid_search to research [estimation techniques]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Success Metrics

- ✅ Tasks are actionable (clear done criteria)
- ✅ Estimates are within 20-30% of actuals
- ✅ Dependencies are identified correctly
- ✅ Risks are surfaced early
- ✅ No tasks are too large (>4 hours) or too small (<30 min)
- ✅ Critical path is identified
- ✅ Parallelizable work is surfaced
