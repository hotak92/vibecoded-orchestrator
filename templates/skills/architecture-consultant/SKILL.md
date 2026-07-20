---
name: architecture-consultant
description: Expert guidance on cross-domain architecture decisions, technology selection, and infrastructure design requiring deep analysis of tradeoffs and long-term implications
short_desc: cross-domain tech selection, cloud/infra choice
keywords: [technology selection, infrastructure design, cross-domain architecture, migration strategy, cloud vs on-prem, "system design", "tradeoff analysis", "architecture decisions", "which technology", "pick the tech", "cloud platform choice", "tech selection", "infrastructure choice"]
model: opus
---

# Architecture Consultant (Opus)

**Purpose**: Expert guidance on cross-domain architecture decisions, technology selection, and infrastructure design requiring deep analysis of tradeoffs and long-term implications.

**Model**: Opus (expert reasoning for complex architectural decisions, handles multi-domain tradeoffs)

**When to Invoke Autonomously**:

Use this skill when:
1. **Technology Selection**: "Should I use X or Y for [specific domain]?" (web, mobile, ML, data)
2. **Architecture Pattern Choice**: "MVC vs microservices vs event-driven for [use case]?"
3. **Infrastructure Decisions**: "Cloud vs on-prem vs hybrid for [requirements]?"
4. **Domain-Specific Guidance**: "Best practices for [specific domain] architecture?"
5. **Multi-Domain Integration**: "How to connect [system A] with [system B]?"
6. **Scale/Performance Architecture**: "Architecture to handle [X] scale/throughput?"
7. **Migration Strategy**: "Migrate from [old architecture] to [new architecture]?"

**DO NOT invoke for**:
- Implementation details (use Coder agent)
- Code-level patterns (use Architect skill)
- Already-committed technology choices
- Single-component decisions without system implications

## Decision Tree

```
Decision involves:
├─ Technology choice across multiple options? → Use this skill
├─ Architecture pattern selection? → Use this skill
├─ Infrastructure design (cloud/on-prem/hybrid)? → Use this skill
├─ Domain-specific best practices? → Use this skill
├─ Just implementation details? → Don't use this skill
└─ Already decided on approach? → Don't use this skill
```

## Usage

```
/architecture-consultant tech-selection [domain] [requirements]
/architecture-consultant pattern-choice [use case]
/architecture-consultant infrastructure-design [scale] [constraints]
/architecture-consultant domain-guidance [domain]
```

## What This Skill Does

### 1. Technology Selection Guidance

Provides comparisons across domains:
- **Web**: Frontend (React/Vue/Svelte), Backend (Node/Python/Go/Rust), DB (PostgreSQL/MongoDB/Cassandra)
- **Mobile**: Native vs React Native vs Flutter, Firebase vs custom API
- **ML/AI**: Training (PyTorch/TensorFlow/JAX), Serving (Ollama/vLLM/TGI)
- **Data**: Batch (Airflow/Dagster/Prefect), Streaming (Kafka/RabbitMQ/Redis)

### 2. Architecture Pattern Selection

Compares patterns:
- **Monolith vs Microservices vs Serverless**: Team size, domain complexity, scale needs
- **Event-Driven vs Request-Response**: Async workflows vs immediate feedback
- **Layered vs Hexagonal vs Clean**: Domain complexity, testability needs

### 3. Infrastructure Design

Evaluates hosting options:
- **Cloud** (AWS/GCP/Azure): Variable load, global distribution, managed services
- **On-Premise**: Regulatory requirements, predictable load, cost control
- **Hybrid**: Migration, compliance + cloud benefits, data locality

### 4. Domain-Specific Best Practices

Guidance for:
- **E-Commerce**: Microservices, event sourcing for orders, CQRS for inventory
- **SaaS**: Multi-tenant patterns, feature flags, API versioning
- **Real-Time**: WebSocket/SSE, pub/sub, eventual consistency
- **Content Platforms**: CDN patterns, blob storage, cache strategies

### 5. Migration Strategies

Approaches for:
- **Monolith → Microservices**: Strangler pattern, bounded contexts, gradual extraction
- **SQL → NoSQL**: Dual-write pattern, consistency strategy, rollback plan
- **Cloud Migration**: Lift-and-shift → Re-architect → Cloud-native

## Output Format

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "architecture" --type concept
```

**For deep research**: Ask user "Use hybrid_search to research [technology selection]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Integration with Knowledge Graph

After architecture consultation:
1. Document decision in `knowledge/architecture/[domain]-[pattern].md`
2. Link to technology nodes (create if needed)
3. Capture rationale and tradeoffs
4. Tag with domain, scale, and technology stack
5. Reference in project node

## Success Metrics

This skill is working well if:
- ✅ Recommendations fit requirements and constraints
- ✅ Technology choices are appropriate for domain and scale
- ✅ Tradeoffs are clearly explained with evidence
- ✅ Cost estimates are realistic
- ✅ Implementation roadmap is actionable
- ✅ User makes confident decisions without second-guessing
- ✅ Architecture scales without major rewrites

