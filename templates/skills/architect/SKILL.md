---
name: architect
description: Design complex system architectures, evaluate tradeoffs, and make critical technical decisions requiring deep reasoning
model: opus
---

# Architect - Expert System Design (Opus)

**Purpose**: Design complex system architectures, evaluate tradeoffs, and make critical technical decisions requiring deep reasoning.

**Model**: Opus 4.5 (expert reasoning, handles ambiguity, considers edge cases)

**When to Invoke Autonomously**:

Use this skill when:
1. **Complex System Design**: Multi-component system with >5 moving parts
2. **Critical Tradeoffs**: Multiple valid approaches with significant pros/cons
3. **Unclear Requirements**: Ambiguous user needs requiring clarification and proposal
4. **Integration Challenges**: Connecting disparate systems with compatibility concerns
5. **Performance/Scale Decisions**: Optimization choices affecting throughput, latency, cost
6. **Security/Reliability**: Decisions impacting system safety, data integrity, fault tolerance

**DO NOT invoke for**:
- Simple CRUD operations or straightforward implementations
- Well-defined patterns with obvious solutions
- Refactoring existing code without architectural changes
- Tasks where requirements are crystal clear and non-controversial

## Decision Tree

```
Is this task:
├─ Simple implementation? → Don't use this skill, just code
├─ Requires choosing between 3+ approaches? → Use this skill
├─ Involves system-wide changes? → Use this skill
├─ Has unclear requirements? → Use this skill
└─ Standard CRUD/patterns? → Don't use this skill
```

## Usage

```
/architect design a [system/feature] that [requirements]
/architect evaluate [approach A] vs [approach B] for [use case]
/architect review [existing architecture] and suggest improvements
```

## What This Skill Does

### 1. Requirements Clarification
- Asks probing questions to uncover hidden requirements
- Identifies constraints (performance, cost, time, team skills)
- Clarifies non-functional requirements (scalability, security, maintainability)

### 2. Architecture Design
- Proposes 2-3 alternative approaches with detailed pros/cons
- Considers edge cases and failure modes
- Documents assumptions and decision rationale
- Creates high-level component diagrams (text-based)

### 3. Technology Selection
- Evaluates tools/frameworks/libraries against requirements
- Considers team expertise, community support, maturity
- Flags vendor lock-in risks and migration paths

### 4. Tradeoff Analysis
- Performance vs complexity
- Cost vs scalability
- Time-to-market vs technical debt
- Build vs buy decisions

### 5. Risk Assessment
- Identifies technical risks and mitigation strategies
- Estimates complexity and implementation timeline
- Flags knowledge gaps requiring research/prototyping

## Output Format

See [template.md](template.md) for architecture proposal structure.

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "architecture" --type concepts
```

**For deep research**: Ask user "Use hybrid_search to research [system design]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Integration with Knowledge Graph

After completing architecture work:
1. Document decision in knowledge graph node (e.g., `knowledge/concepts/[architecture-pattern].md`)
2. Link to relevant existing patterns and tools
3. Tag with domain (#architecture, #system-design, #tradeoffs)
4. Capture "why we chose X over Y" for future reference

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- Known terms → `kg-search` CLI (fast, ~100ms)
- Conceptual → `search_knowledge_graph` or `hybrid_search` MCP
- Relationships → `semantic_graph_search` MCP
- Code by purpose → `search_code_graph` MCP
- Quick analysis (FREE) → `chat` Ollama MCP
- Literal strings → Grep
## Supporting Files

- **Template**: Use [template.md](template.md) for architecture proposal format
- **Examples**: See [examples/](examples/) for good/bad architecture decisions

## Success Metrics

This skill is working well if:
- ✅ Architecture proposals are complete and actionable
- ✅ Tradeoffs are clearly explained with evidence
- ✅ User makes confident decisions based on analysis
- ✅ Proposed solutions handle edge cases and scale requirements
- ✅ Implementations follow the architecture without major surprises

