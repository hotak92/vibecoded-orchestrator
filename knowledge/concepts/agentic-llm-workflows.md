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
updated: 2026-04-05T14:33:08Z
valid_from: 2026-01-28 19:00:00+00:00
valid_until: null
status: active
---

# Agentic LLM Workflows

Pattern for using multiple LLM agents with specialized roles and different models to accomplish complex tasks efficiently.

## Purpose
Enable cost-effective, high-quality AI workflows by routing tasks to appropriate models based on complexity, while using specialized agents for different roles.

## Links
- [[implements::Claude Workflow]] - Implementation of this concept
- [[implements::Model Strategy]] - Cost optimization approach
- [[implements::Agent Orchestration]] - Coordination patterns
- [[uses::Claude Orchestrator]] - Uses this workflow

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
Dedicated agents for specific roles:

**Planning Agents**:
- `orchestrator` - Workflow coordination
- `planner` - Requirements and design
- `architect` - Architecture decisions (Opus)

**Implementation Agents**:
- `coder` - Code implementation
- `helper-scripter` - Automation scripts
- `tester` - Test creation and verification

**Maintenance Agents**:
- `memory-manager` - Context management
- `doc-maintainer` - Documentation sync
- `code-reviewer` - Quality assurance (Opus)

### 3. Workflow Orchestration
Orchestrator coordinates agent interactions:

**Simple Flow**:
```
coder → tester → memory-manager
```

**Complex Flow**:
```
orchestrator → memory-manager → planner →
coder → tester → doc-maintainer → memory-manager
```

**Parallel Flow**:
```
orchestrator → [agent-1, agent-2, agent-3] → aggregator
```

### 4. Context Management
Efficient context sharing between agents:

- **Concise handoffs**: 300-500 tokens between agents
- **File references**: Use paths instead of full content
- **State tracking**: CONTEXT_STATE.md for current task
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
- `/task` - Complete task workflow
- `/context` - Context management
- Custom skills per project

## Best Practices

### Agent Selection
1. Default: Direct implementation (no agent spawn)
2. Complex task: Spawn appropriate agent
3. Critical decision: Use Opus agent
4. Parallel work: Spawn multiple agents

### Context Efficiency
1. Keep CONTEXT_STATE.md under 325 lines
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
**Solution**: Orchestrator agent for complex workflows

**Challenge**: Cost tracking
**Solution**: Model strategy documentation, usage monitoring

**Challenge**: Quality consistency
**Solution**: Quality gates via hooks, code review agent

## Related Patterns
- **Chain of Thought** - Sequential reasoning
- **Tree of Thought** - Parallel exploration
- **Reflexion** - Self-reflection and improvement
- **React** - Reasoning and acting loop

## Research References
[TBD - User will provide papers on agentic LLM use]

