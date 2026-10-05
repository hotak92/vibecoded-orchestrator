# Prompt engineering field guide

Specialisation depth for writing and tuning prompts, agent definitions, and
skill descriptions. Read this when an LLM prompt underperforms, output format
drifts, or a description needs tuning for auto-invocation.

## Core patterns

1. **Few-shot examples** — show 2–5 input→output examples; the single most
   reliable lever for format and style conformance.
2. **Chain-of-thought** — ask for step-by-step reasoning on tasks that need
   it; large error reduction on complex code reasoning. Skip for trivial
   lookups (it just costs tokens).
3. **Output format specification** — explicit structure with one worked
   example beats prose adjectives ("concise", "well-formatted").
4. **Constraint enforcement** — explicit boundaries and DO-NOT statements;
   repeat the constraints that matter most near the end of long prompts.
5. **Role-based prompting** — "You are a [role] specializing in [domain]"
   measurably improves domain accuracy.

The three pillars: **Context** (what the model needs to know about the
codebase/task), **Clarity** (explicit, unambiguous requirements),
**Constraints** (technical boundaries: language, framework, patterns, output
shape).

## Common issues and fixes

| Symptom | Fix |
|---|---|
| Inconsistent output | Explicit format + few-shot examples |
| Over-verbose | Length/structure constraints |
| Hallucinations | Constrain to provided context; require citations |
| Ignoring instructions | Repeat key constraints; move them later; add examples |
| Wrong level of abstraction | Name the audience and the artifact shape |

## Agent/skill description tuning (auto-invocation)

The `description:` frontmatter field is what Claude matches against when
deciding to delegate to an agent or load a skill:

1. Third person, non-empty, short. "Processes X and generates Y" — never
   "I can help you...".
2. State WHAT it does + WHEN to delegate, including trigger phrases users
   naturally type. Add a when-NOT clause if easily confused with a sibling.
3. Vague descriptions never trigger: "Helps with documents" fails;
   "Extract text and tables from PDF files. Use when working with PDFs,
   forms, or document extraction" works.
4. "Use proactively" encourages proactive delegation — use it only where
   that is wanted.
5. Not triggering? Add the missing trigger phrases. Triggering too often?
   Narrow the WHEN clause and add when-NOT.
6. Keep detail OUT of descriptions — combined descriptions across all agents
   load at startup and have a budget; operational detail belongs in the body,
   which loads only when the agent runs.

## Model-tier awareness

Optimize for the target tier: terse, mechanical, example-heavy prompts for
small/fast models; richer context and open reasoning for large tiers. A
prompt that works on a big model may need explicit step decomposition on a
small one.

## Related

- Authoring agent/skill files: the `agent-author` agent.
- Reviewing prompts in code (LLM pipelines): `ai-llm-expert` agent in the
  `ai-engineering` pack.
