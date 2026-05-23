---
name: performance-optimizer
description: Cross-domain performance analysis and optimization guidance (frontend render, backend queries, AI model inference)
short_desc: perf analysis: frontend, backend, AI inference
keywords: [performance optimization, render performance, query optimization, inference latency, bottleneck, "query performance", slow, "slow app", "make it faster", "performance issue", latency, "optimize performance", "speed up"]
model: sonnet
---

# Performance Optimizer (Sonnet)

**Purpose**: Cross-domain performance analysis and optimization guidance (frontend render, backend queries, AI model inference).

**Model**: Sonnet 4.5 (balanced reasoning for profiling and optimization)

## When to Invoke Autonomously

Use this skill when:
1. **User Reports Slowness**: "The app is slow", "queries take forever", "inference is too slow"
2. **Before Production Deploy**: Proactive performance check on critical paths
3. **Resource Constraints**: High CPU/memory/VRAM usage without clear cause
4. **Scaling Issues**: Performance degrades with load/data size
5. **After Major Changes**: Refactoring that might impact performance

## DO NOT invoke for

- Obvious inefficiencies (N+1 queries, missing indexes - just fix them)
- Performance isn't a concern (prototypes, simple scripts)
- User explicitly prioritizes speed-to-market over optimization

## Decision Tree

```
Performance issue in:
├─ Frontend (render, bundle)? → Use this skill
├─ Backend (queries, API)? → Use this skill
├─ AI (inference, VRAM)? → Use this skill
├─ Obvious fix (N+1 query)? → Just fix it
└─ Not performance-critical? → Skip optimization
```

## Usage

```
/performance-optimizer analyze frontend [component/page]
/performance-optimizer analyze backend [endpoint/query]
/performance-optimizer analyze ai [model/inference]
/performance-optimizer profile [specific operation]
```

## What This Skill Does

**Cross-Domain Performance Analysis**:
- Frontend: Bundle size, render performance, lazy loading, memoization
- Backend: Database queries, caching, connection pooling, async processing
- AI/ML: Model quantization, batch processing, VRAM optimization, context caching

**Bottleneck Identification**:
- Profile to find hotspots (CPU, memory, database, network)
- Measure baseline performance (response time, throughput)
- Prioritize high-impact optimizations (80/20 rule)

**Optimization Recommendations**:
- Specific techniques (code patterns, configuration changes)
- Expected improvements (quantified estimates)
- Tradeoff analysis (quality vs speed, cost vs performance)

**Testing & Validation**:
- Load testing strategy (realistic traffic, peak load)
- Performance targets by domain (FCP <1.5s, API <200ms, etc.)
- Monitoring setup to track improvements

**See**: `examples/optimization-checklist.md` for domain-specific checklists, `examples/optimization-patterns.md` for common patterns and code examples

## Quick Workflow Reference

**Before optimizing**: Search for proven optimization patterns
```bash
.claude/scripts/kg-search search "performance" --type concepts
```

**For deep research**: Ask user "Use hybrid_search to research [optimization techniques]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Success Metrics

- ✅ Identifies actual bottlenecks (not guesses)
- ✅ Recommendations are actionable and prioritized
- ✅ Expected improvements are realistic
- ✅ Optimizations improve performance measurably
- ✅ Tradeoffs are clearly explained
