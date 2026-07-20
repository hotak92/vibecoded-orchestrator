---
title: Claude Code Workflows Feature — Premade Workflow Integration
type: concept
tags: [claude-code, workflows, ultracode, multi-agent, orchestration, bundle, mid-level-architecture]
created: 2026-06-11T00:00:00Z
updated: 2026-07-20T00:00:00Z
valid_from: 2026-06-11T00:00:00Z
valid_until: null
status: active
---

# Claude Code Workflows Feature — Premade Workflow Integration

Claude Code v2.1.154+ (2026-05-28) ships a built-in `Workflow` tool: deterministic JavaScript orchestration scripts that spawn subagents via `agent()`, `parallel()`, `pipeline()`, `phase()`, `log()`, with a `budget` token tracker and `args` parameterization. Official docs: code.claude.com/docs/en/workflows.md.

## Key facts

- **Availability**: all paid plans (Pro, Max, Team, Enterprise), Anthropic API, and Bedrock / Vertex / Foundry. On Pro, turn them on from the Dynamic workflows row in `/config`. Disable via `disableWorkflows: true` (settings) or `CLAUDE_CODE_DISABLE_WORKFLOWS=1`.
- **Triggers**: the `ultracode` keyword in a prompt (single-task opt-in), `/effort ultracode` (session-wide: Claude plans a workflow for each substantive task), natural-language "use a workflow", or saved workflows invoked as `/<name>`. `ultracode` is the literal keyword from v2.1.160 onward (it was `workflow` before that); natural-language requests work in both.
- **Bundled**: `/deep-research <question>` ships built-in — fans out web searches across angles, cross-checks sources, votes on each claim, returns a cited report with unsupported claims filtered out. Requires the WebSearch tool.
- **Saved workflows**: `.claude/workflows/<name>.{mjs,js}` (project scope — shareable in a repo, wins over user scope) or `~/.claude/workflows/` (user scope). Save a completed run's script via `s` in the `/workflows` view. Must start with a pure-literal `export const meta = {name, description, phases?}` (extracted without execution).
- **Limits**: up to 16 concurrent agents (fewer on low-core machines), 1000 agents per run; subagents always run `acceptEdits` and inherit the session tool allowlist regardless of the session's permission mode; no mid-run user input; resume replays cached completed-agent results within the same session (a fresh session restarts the run).
- **Determinism**: the runtime blocks `Date.now()` and `Math.random()`; the script has no direct filesystem or shell access (agents do the I/O, the script coordinates).
- **Primitives**: `agent(prompt, opts)` runs one subagent (pass a `schema` for validated structured output); `parallel(thunks)` is a barrier (waits for all); `pipeline(items, ...stages)` streams each item through stages with no barrier; `phase()`, `log()`, and a token `budget` round out the API. `args` is exposed as a global for parameterized saved workflows.
- **Custom agent types**: `agent(prompt, {agentType: '<registered-agent>'})` resolves from the same registry as the Agent tool — bundled orchestrator agents are usable as workflow stages.

## Orchestrator integration design (per-item shipped status)

What has SHIPPED from this design: the `detect-workflow-needs` / `generate-workflow` CLI tools (`templates/scripts/`) and the `agent-skill-keyword-match.py` workflow-scan extension (item 4 under Discoverability below; covered by `tests/test_keyword_match_workflows.py`). Items 1-4 in this list remain design candidates, NOT shipped:

1. **`templates/workflows/`** — NOT SHIPPED (no such bundle category exists on disk). Design: installed to each project's `.claude/workflows/`, manifest-tracked with the same UPSERT + user-modified-preserve discipline as agents/skills/hooks.
2. **Launcher per-project "Workflows" tab** — NOT SHIPPED (no `project_workflows` table exists). Design: enabled toggles mirroring the Agents/Skills pattern (disable ≠ delete, rows survive bundle updates).
3. **Candidate premade workflows** — NOT SHIPPED (depends on item 1). Each encodes a fan-out the orchestrator's workflow docs already prescribe manually:
   - `vco-release-audit` — per-change diff-vs-plan review + secret/leak-hygiene scan + cross-OS `.sh`/`.ps1` sibling check.
   - `vco-kg-audit` — KG drift sweep: nodes pointing at deleted files, stale validity windows, duplicate candidates.
   - `vco-bug-hunt` — loop-until-dry finder pool + adversarial verification panel.
   - `vco-doc-sweep` — documentation duplicate/archive/root-hygiene fan-out.
   - `vco-onboard` — parallel-reader codebase map for newly-installed projects.
4. **KG synergy** — design note (applies once items 1-3 ship): workflow agents reach MCP tools (ToolSearch-resolved), so synthesis stages can call `store_knowledge_node` and persist findings into the project KG automatically.

## Discoverability — ship workflows with their router, not alone

Claude will not spontaneously run a workflow for users who don't know the feature exists (the Workflow tool's opt-in gate excludes "task would merely benefit"; the prescribed fallback is describe-and-ask). And on a generic "use a workflow" request, Claude may author a fresh script rather than reuse a saved one. Bundle three pieces together:
1. Workflow files with strong `meta.description` + `meta.whenToUse` (these route selection).
2. **Skill wrappers**: a skill whose instructions say "call Workflow({name: ...})" is a legitimate opt-in path — users invoke a familiar slash command and get the orchestration without knowing the underlying feature.
3. **CLAUDE.md router rules**: "when a task matches a `.claude/workflows/` entry, offer it by name with a rough cost estimate" and "on workflow opt-in, check `.claude/workflows/` for a match before authoring a new script". Reliability must be structural, not dependent on per-session model discipline.
4. **Keyword-suggest hook extension** (SHIPPED): `templates/scripts/agent-skill-keyword-match.py` also scans `.claude/workflows/*.{mjs,js}` and inject an offer-worded suggestion (never auto-launch — workflows are user-opt-in token spend). **Canonical keyword carrier: a `keywords: [...]` array inside the workflow's `meta` block** — the runtime tolerates the extra meta key (verified 2026-06-11 with a zero-agent probe), so each shipped workflow declares its own trigger phrases in-file and matching stays fully dynamic (no hardcoded registry). Keyword discipline: multi-word, high-specificity phrases only — single generic keywords are the dominant source of false-positive suggestions in the agent/skill matcher.

[[relatedTo::Agentic-Coding-Workflow]]
