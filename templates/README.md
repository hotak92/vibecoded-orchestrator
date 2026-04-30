# Templates — Agents and Skills

This directory holds the agents and skills that `install.py` copies into a user's `.claude/agents/` and `.claude/skills/` at install time. Contents here are *templates*, not active agents — they're populated into your project's `.claude/` so Claude Code picks them up.

## Bundled agents — `agents/free/` (29 agents)

All bundled agents are free and installed by default. They cover the base orchestrator workflow plus specialist + coordinator roles: coding, testing, planning, docs, knowledge graph, code graph, migration, bootstrapping, multi-agent design, and language/framework expertise.

| Agent | Role |
|---|---|
| `coder` | Implementation |
| `tester` | Test creation + bug investigation |
| `planner` | Requirements + task breakdown |
| `helper-scripter` | Scaffolds scripts, hooks, skills |
| `doc-extractor` | Read-only knowledge extraction |
| `doc-maintainer` | Doc updates with archival pipeline |
| `doc-organizer` | Folder hygiene, duplicate prevention |
| `kg-navigator` | KG read-only exploration |
| `knowledge-curator` | KG node writes + cross-references |
| `graph-health-checker` | KG + code graph integrity |
| `code-graph-updater` | Incremental code graph sync |
| `code-migrator` | Language/framework migration |
| `prompt-engineer` | Review + optimize agent prompts |
| `orchestrator-installer` | Infrastructure bring-up |
| `project-bootstrapper` | New-project scaffolding |
| `project-migrator` | Adopt orchestrator into existing project |
| `ai-agentic-architect` | Designs multi-agent workflows |
| `project-coordinator` | Coordinates running agents |
| `project-architect` | End-to-end design |
| `project-organizer` | Cross-agent project health |
| `expert-coder` | Opus-powered implementation for complex features |
| `ai-llm-expert` | LLM integration specialist |
| `backend-specialist` | Server/API/database |
| `frontend-specialist` | React/Vue/UI |
| `gui-expert` | Gradio + GUI/UX |
| `deep-researcher` | Recursive sub-agent research |
| `code-explorer` | Read-only code exploration |
| `gui-tester` | GUI testing |
| `web-explorer` | Web research |

### Skills — `skills/` (29 skills)

All shipped in free tier. Short-form guidance documents invoked via `/skill-name`. Organized alphabetically: `accessibility-checker`, `ai-model-selector`, `ai-prompting`, `ai-rag-advisor`, `api-designer`, `architect`, `architecture-consultant`, `code-review-expert`, `context`, `context-compress`, `database-advisor`, `debug-expert`, `deployment-advisor`, `doc-organizer`, `doc-template`, `explore-codebase`, `extract-docs`, `fix-issue`, `gui-test`, `gui-ux-expert`, `hardware-calculator`, `interview`, `kg-research`, `performance-optimizer`, `react-patterns`, `security-reviewer`, `task-breakdown`, `tdd`, `workflow-maintain`.

## Other paid modules (not agents)

Other paid features are NOT shipped in this repo. Paid-module code is hosted separately — it is fetched at runtime by the VCT Launcher after license verification and installed into the user's project. **Code is not on the user's machine if they haven't purchased the module.**

Known paid modules, handled outside this repo:

- **Coordination (Supabase backend)** — team coordination MCP with persistent cloud backend. Paid add-on.
- **RL retrieval reranking** — per-project reranker trained over KG/codegraph retrieval. Paid add-on.

The VCT Launcher is the gate: it validates the license, fetches the module from a private distribution server, and installs into `.claude/` or `claude_mcp_servers/`. The free repo only documents these modules' existence for discoverability, never their source.

## Path placeholders

Template files may contain these placeholders. `install.py` substitutes them at copy time:

| Placeholder | Expands to |
|---|---|
| `{{ORCHESTRATOR_ROOT}}` | Where you installed the orchestrator (e.g., `/home/you/vibecoded-orchestrator`) |
| `{{PROJECTS_ROOT}}` | Parent of the orchestrator dir |
| `{{HOME}}` | Your `$HOME` |

Keep placeholders in templates — do NOT hard-code paths.

## Install flags

```bash
python install.py                      # 29 bundled agents + all skills (default)
python install.py --no-agents          # skip agent installation
python install.py --no-skills          # skip skill installation
```

Reinstalls preserve any agents/skills already present — you won't lose customizations.

## Adding your own agents

1. Drop `.md` files directly in `.claude/agents/` (not `templates/agents/`) — those are yours, not managed by install.py.
2. If you want your agent to be reinstalled on fresh installs, contribute it to `templates/agents/free/` in a PR.
