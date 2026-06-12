---
name: code-review-expert
description: Deep code analysis identifying subtle bugs, security issues, performance problems, and architectural concerns requiring expert-level reasoning
short_desc: "deep Opus review for subtle bugs and architecture"
keywords: ["code review", "subtle bug", "production code", "security audit", "pre-release review", "edge case", "race condition", OWASP, "review my code", "review this PR", "peer review", "pull request review", "PR review", "code quality"]
model: opus
---

# Code Review Expert (Opus)

**Purpose**: Deep code analysis identifying subtle bugs, security issues, performance problems, and architectural concerns requiring expert-level reasoning.

**Model**: Opus 4.5 (detects non-obvious issues, understands system-wide implications)

**When to Invoke Autonomously**:

Use this skill when:
1. **Critical Code**: Security-sensitive, performance-critical, or production-impacting changes
2. **Large Changeset**: >500 lines or touches >5 files
3. **Complex Logic**: Concurrent code, algorithms, state machines, error handling
4. **Pre-Release Review**: Before merging major features or deploying to production
5. **Architectural Impact**: Changes affecting system design or API contracts
6. **Unknown Codebase**: Reviewing unfamiliar code or inherited projects

**DO NOT invoke for**:
- Simple typo fixes or formatting changes
- Single-line changes with obvious intent
- Documentation updates
- Automated refactoring by trusted tools

## Decision Tree

```
Code change is:
├─ <50 lines, simple logic? → Quick review yourself
├─ Security/performance critical? → Use this skill
├─ >500 lines or complex logic? → Use this skill
├─ Pre-release major feature? → Use this skill
├─ Just formatting/docs? → Don't use this skill
└─ Touches core architecture? → Use this skill
```

## Usage

```
/code-review-expert review [file/directory]
/code-review-expert security-audit [component]
/code-review-expert performance-analysis [module]
```

## What This Skill Does

### 1. Bug Detection
- Off-by-one errors, race conditions, resource leaks
- Null pointer dereferences, type mismatches
- Edge cases: empty inputs, boundary values, overflow
- Logic errors in conditionals, loops, state transitions

### 2. Security Analysis
- SQL injection, XSS, CSRF vulnerabilities
- Authentication/authorization bypasses
- Sensitive data exposure (logs, errors, responses)
- Cryptographic issues (weak algorithms, hardcoded keys)
- Input validation gaps

### 3. Performance Issues
- N+1 queries, inefficient algorithms
- Unnecessary allocations, memory leaks
- Blocking operations in async code
- Missing indexes, unbounded loops

### 4. Code Quality
- Maintainability: readability, naming, structure
- Testability: tight coupling, hidden dependencies
- Error handling: missing checks, poor recovery
- Documentation: misleading comments, missing context

### 5. Architectural Concerns
- Abstraction violations, tight coupling
- Inconsistent patterns compared to codebase
- Missing extensibility for known requirements
- Technical debt introduction

## Output Format

See [template.md](template.md) for code review report structure.

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "code-review" --type concepts
```

**For deep research**: Ask user "Use hybrid_search to research [security patterns]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- Known terms → `kg-search` CLI (fast, ~100ms)
- Conceptual → `hybrid_search` MCP
- Relationships → `semantic_graph_search` MCP
- Code by purpose → `search_code_graph` MCP
- Quick analysis: use Claude directly (Ollama MCP removed in v0.2.11 as redundant)
- Literal strings → Grep
## Integration with Knowledge Graph

After reviewing code:
1. If novel pattern found: Document in `knowledge/patterns/[pattern-name].md`
2. If common mistake: Add to `knowledge/anti-patterns/[mistake-name].md`
3. Link to relevant security/performance concepts
4. Tag with domain and severity

## Supporting Files

- **Template**: Use [template.md](template.md) for review report format
- **Checklist**: See [examples/review-checklist.md](examples/review-checklist.md) for comprehensive review criteria

## Success Metrics

This skill is working well if:
- ✅ Catches critical bugs before production
- ✅ Identifies security vulnerabilities consistently
- ✅ Provides actionable, specific recommendations
- ✅ Reviews are thorough but not pedantic
- ✅ Feedback improves code quality measurably

