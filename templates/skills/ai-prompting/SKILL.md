---
name: ai-prompting
description: Quick tips and templates for effective prompt engineering - few-shot examples, chain-of-thought patterns, constraint specification, output formatting
short_desc: quick prompting templates and tips
keywords: ["few-shot examples", "constraint specification", "output formatting", "prompt template", "prompting tips", "prompt patterns", "prompt engineering", "improve this prompt", "write a prompt"]
model: haiku
---

# AI Prompting (Haiku)

**Purpose**: Quick tips and templates for effective prompt engineering - few-shot examples, chain-of-thought patterns, constraint specification, output formatting.

**Model**: Haiku 4.5 (fast, practical prompt improvement)

**When to Invoke Autonomously**:

Use this skill when:
1. **Prompt Not Working Well**: "LLM output is inconsistent/poor quality"
2. **Need Examples**: "How to structure few-shot prompt for [task]?"
3. **Output Formatting**: "LLM not following desired format"
4. **Constraint Specification**: "How to enforce constraints in prompt?"
5. **Chain-of-Thought**: "Need step-by-step reasoning for [complex task]"

**DO NOT invoke for**:
- Complex prompt engineering (use AI LLM Expert agent)
- Production prompt systems (use AI LLM Expert agent)
- Already-working prompts

## Usage

```
/ai-prompting improve-prompt [current prompt] [issue]
/ai-prompting few-shot [task description]
/ai-prompting chain-of-thought [reasoning task]
/ai-prompting output-format [desired structure]
```

## Quick Prompt Patterns

For detailed examples of each pattern, see [examples/prompt-patterns.md](examples/prompt-patterns.md).

### Common Patterns

1. **Few-Shot Examples**: Show 2-5 input→output examples (15-30% accuracy improvement)
2. **Chain-of-Thought**: "Think step-by-step" for reasoning tasks
3. **Output Format Specification**: Explicit structure with examples
4. **Constraint Enforcement**: Explicit boundaries, "DO NOT" statements
5. **Role-Based Prompting**: "You are a [role] specializing in [domain]" (20-40% improvement)

### Common Issues & Fixes

- **Inconsistent Output** → Explicit format + examples
- **Over-Verbose** → Length constraints
- **Hallucinations** → Constrain to provided context only
- **Ignoring Instructions** → Repeat constraints, add examples

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "prompt-engineering" --type concepts
```

**For deep research**: Ask user "Use hybrid_search to research [prompting techniques]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Integration with Knowledge Graph

After prompt improvement:
1. Document successful pattern in `knowledge/prompts/[use-case]-patterns.md`
2. Tag with use case and effectiveness

## Supporting Files

- **Prompt Patterns**: See [examples/prompt-patterns.md](examples/prompt-patterns.md) for detailed examples
- **Template**: Use [template.md](template.md) for structured prompt improvements

## Success Metrics

- ✅ Prompt output improves after applying pattern
- ✅ Tips are actionable and immediate
- ✅ Common issues resolved
