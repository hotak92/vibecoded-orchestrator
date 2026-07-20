---
title: Agentic LLM Workflows
type: concept
tags:
- concept
- AI
- agents
- workflow
- cost-optimization
- high-level-concept
- ML
- mid-level-architecture
- typescript
created: 2026-01-28 19:00:00+00:00
updated: 2026-07-20T00:00:00Z
valid_from: 2026-01-28 19:00:00+00:00
valid_until: null
status: active
---

# Agentic LLM Workflows

Pattern for using multiple LLM agents with specialized roles and different models to accomplish complex tasks efficiently.

## Purpose
Enable cost-effective, high-quality AI workflows by routing tasks to appropriate models based on complexity, while using specialized agents for different roles.

## Links
- [[implements::Model Strategy]] - Cost optimization approach
- [[implements::Agent Orchestration]] - Coordination patterns

## Core Principles

### 1. Model Selection Strategy
Match model capability and cost to task complexity:

**Haiku** (Fast, cheap):
- Simple validation checks
- Boolean questions
- Text formatting
- Routine operations
- ~70% of tasks

**Sonnet** (Balanced):
- Planning and design
- Code implementation
- Complex reasoning
- ~25% of tasks

**Opus** (Premium):
- Critical architecture decisions
- Complex code review
- High-stakes choices
- ~5% of tasks

**Ollama** (Free):
- Simple validations
- Summary generation
- Text extraction
- When available

### 2. Agent Specialization
Dedicated agents for specific role archetypes (exact names vary per install — check the bundled roster in `.claude/agents/` and `.claude/skills/`):

**Planning roles**:
- a planning agent — requirements analysis and design (e.g. `planner`)
- an architecture consultant — high-stakes design decisions on a premium model tier (often shipped as a skill, e.g. `/architect`)

**Implementation roles**:
- a coding agent — code implementation (e.g. `coder`)
- an automation-script agent — helper scripts and tooling (e.g. `helper-scripter`)
- a testing agent — test creation and verification (e.g. `tester`)

**Maintenance roles**:
- a documentation-sync agent (e.g. `doc-maintainer`)
- context/working-memory curation — typically the main session itself, via the working-memory-document discipline
- a code-review capability on a premium tier (e.g. a `code-review-expert` skill)

### 3. Workflow Orchestration
The main session (acting as coordinator) sequences agent interactions:

**Simple Flow**:
```
coder → tester → doc-maintainer
```

**Complex Flow**:
```
coordinator → planner → coder → tester → doc-maintainer
(coordinator updates working memory between phases)
```

**Parallel Flow**:
```
coordinator → [agent-1, agent-2, agent-3] → aggregator
```

### 4. Context Management
Efficient context sharing between agents:

- **Concise handoffs**: 300-500 tokens between agents
- **File references**: Use paths instead of full content
- **State tracking**: a single working-memory doc per task
- **Cleanup**: Archive completed work

## Benefits

### Cost Reduction
- 70% cost savings vs. always using premium model
- Only pay for quality when needed
- Free models (Ollama) for simple tasks

### Quality Improvement
- Specialized agents excel in their domain
- Premium models for critical decisions only
- Iterative refinement through multiple agents

### Scalability
- Parallel agent execution
- Independent task processing
- Modular agent addition/removal

### Flexibility
- Skip phases for simple tasks
- Direct agent invocation when appropriate
- Adapt workflow to task complexity

## Implementation Patterns

### Agent Files
Each agent defined in markdown with:
- Role description
- Model assignment (Haiku/Sonnet/Opus)
- Capabilities and tools
- Usage guidelines

### Hooks
Automatic triggers for quality gates:
- **SessionStart**: Remind about context
- **PreToolUse**: Warnings before actions
- **PostToolUse**: Validation after changes

### Skills
User-invoked workflows:
- Top-level task skill (kick off a full implementation flow)
- Context-management skill (compact / re-inject working memory)
- Custom skills per project

## Best Practices

### Agent Selection
1. Default: Direct implementation (no agent spawn)
2. Complex task: Spawn appropriate agent
3. Critical decision: Use Opus agent
4. Parallel work: Spawn multiple agents

### Context Efficiency
1. Keep per-task working-memory documents short (a few hundred lines max)
2. Archive completed work
3. Use file references, not full content
4. Limit command output verbosity

### Cost Optimization
1. Analyze task complexity before choosing model
2. Batch simple tasks for Haiku
3. Reserve Opus for truly critical decisions
4. Use Ollama when accuracy less critical

## Challenges and Solutions

**Challenge**: Context switching overhead
**Solution**: Concise handoffs, file references

**Challenge**: Agent coordination complexity
**Solution**: Main-session coordination for complex workflows

**Challenge**: Cost tracking
**Solution**: Model strategy documentation, usage monitoring

**Challenge**: Quality consistency
**Solution**: Quality gates via hooks, code-review skill

## Related Patterns
- **Chain of Thought** - Sequential reasoning
- **Tree of Thought** - Parallel exploration
- **Reflexion** - Self-reflection and improvement
- **React** - Reasoning and acting loop

## Research References
[TBD - User will provide papers on agentic LLM use]

