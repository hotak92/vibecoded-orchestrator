---
name: ai-prompting
description: Quick tips and templates for effective prompt engineering - few-shot examples, chain-of-thought patterns, constraint specification, output formatting. Use when an LLM prompt underperforms, output format drifts, or an agent/skill description needs tuning for auto-invocation.
short_desc: quick prompting templates and tips
keywords: ["few-shot examples", "constraint specification", "output formatting", "prompt template", "prompting tips", "prompt patterns", "prompt engineering", "improve this prompt", "write a prompt", "skill description", "agent description"]
model: haiku
---

# AI Prompting (Haiku)

**Purpose**: Quick tips and templates for effective prompt engineering - few-shot examples, chain-of-thought patterns, constraint specification, output formatting.

**Model**: Haiku (fast, practical prompt improvement)

**When to Invoke Autonomously**:

Use this skill when:
1. **Prompt Not Working Well**: "LLM output is inconsistent/poor quality"
2. **Need Examples**: "How to structure few-shot prompt for [task]?"
3. **Output Formatting**: "LLM not following desired format"
4. **Constraint Specification**: "How to enforce constraints in prompt?"
5. **Chain-of-Thought**: "Need step-by-step reasoning for [complex task]"

**DO NOT invoke for**:
- Complex prompt engineering (use the `ai-llm-expert` agent)
- Production prompt systems (use the `ai-llm-expert` agent)
- Authoring or auditing full agent/skill definition files (use the `prompt-engineer` agent)
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

## Agent & Skill Descriptions (Auto-Invocation)

The `description:` frontmatter field is what Claude matches against when deciding to delegate to an agent or load a skill. Quick rules:

1. **Third person, non-empty, max 1,024 characters** — "Processes X and generates Y", never "I can help you..." or "You can use this to...".
2. **State WHAT it does + WHEN to use it**, including trigger keywords users would naturally type. Add a when-NOT clause if the skill/agent is easily confused with a sibling.
3. **Vague descriptions never trigger** — "Helps with documents" fails; "Extract text and tables from PDF files. Use when working with PDFs, forms, or document extraction" works.
4. **Invocation control (skills)**: default = both user and Claude can invoke. `disable-model-invocation: true` removes the description from Claude's context entirely (user-only via `/name`) — never set it on a skill that must auto-invoke. `user-invocable: false` hides it from the `/` menu (Claude-only background knowledge).
5. **VCO extras**: `keywords:` (list) and `short_desc:` feed the UserPromptSubmit keyword-suggester hook — keep keywords literal phrases a user would type, and `short_desc` a one-line scope hint.
6. **Description not triggering?** Add the missing keywords users actually said; **triggering too often?** Make it more specific.

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "prompt-engineering" --type concept
```

**For deep research**: `hybrid_search("[prompting technique]")` (Weaviate MCP)

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
