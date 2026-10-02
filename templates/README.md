# Templates — Agents and Skills

This directory holds the agents and skills that `install.py` copies into a user's `.claude/agents/` and `.claude/skills/` at install time. Contents here are *templates*, not active agents — they're populated into your project's `.claude/` so Claude Code picks them up.

## Bundled agents — `agents/free/` (44 agents)

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
| `orchestrator-installer` | Diagnose + recover partially-failed installs |
| `project-bootstrapper` | Refine bootstrap docs after `--add-project` |
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
| `api-integration-scaffolder` | Typed API clients from specs/docs |
| `automation-engineer` | End-to-end automation workflow design |
| `brand-identity-architect` | Visual brand identity systems |
| `consulting-cto-portfolio-coordinator` | Multi-client consulting portfolio status |
| `consulting-employee-impersonator` | Employee-archetype roleplay output |
| `consulting-sow-drafter` | Statement-of-Work / proposal drafting |
| `discipline-expert` | Cross-disciplinary scientific consultation |
| `enterprise-ux-architect` | IA + interaction design for complex enterprise tools |
| `experiment-designer` | Experiment design from a hypothesis |
| `inbox-triage-operator` | Multi-channel inbox triage + reply drafting |
| `landing-page-critic` | SaaS landing-page evaluation |
| `launch-orchestrator` | Multi-channel release launch content |
| `outbound-sequence-writer` | Cold outreach sequence drafting |
| `paper-triage` | PDF folder triage into structured claims table |
| `postmortem-author` | Blameless post-mortem drafting |
| `sre-incident-responder` | Live production incident triage |

### Skills — `skills/` (54 skills)

All shipped in free tier. Short-form guidance documents invoked via `/skill-name`. Organized alphabetically: `accessibility-checker`, `ai-image-prompting`, `ai-model-selector`, `ai-prompting`, `ai-rag-advisor`, `api-designer`, `architect`, `architecture-consultant`, `batch-image-pipeline`, `build-vs-buy-decision`, `code-review-expert`, `codegraph-diagram`, `consulting-due-diligence`, `consulting-incident-coordinator`, `consulting-portfolio-status`, `content-calendar-planner`, `context`, `context-compress`, `database-advisor`, `debug-expert`, `deployment-advisor`, `design-system-auditor`, `doc-template`, `equation-check`, `explore-codebase`, `extract-docs`, `fix-issue`, `gui-test`, `gui-ux-expert`, `hardware-calculator`, `hpc-submit`, `idempotency-keys`, `interview`, `k8s-manifest-reviewer`, `kg-research`, `performance-optimizer`, `photoshop-scripting`, `react-patterns`, `repro-audit`, `saas-metrics-health-check`, `saas-pricing-strategist`, `sales-call-prep`, `security-reviewer`, `seo-content-brief`, `slo-designer`, `stats-consult`, `structured-output-extraction`, `task-breakdown`, `tdd`, `terraform-plan-reviewer`, `webhook-receiver`, `workflow-cost-estimator`, `workflow-maintain`.

## Other paid modules (not agents)

Other paid features are NOT shipped in this repo. Paid-module code is hosted separately — it is fetched at runtime by the VCT Launcher after license verification and installed into the user's project. **Code is not on the user's machine if they haven't purchased the module.**

Known paid modules, handled outside this repo:

- **RL retrieval reranking** — per-project reranker trained over KG/codegraph retrieval. Paid add-on.

The VCT Launcher is the gate: it validates the license, fetches the module from a private distribution server, and installs into `.claude/` or `claude_mcp_servers/`. The free repo only documents these modules' existence for discoverability, never their source.

## Path placeholders

Template files may contain these placeholders. Every one of them is rendered by
ONE materializer (`vco_lib/materialize.py`) at install and on every update, with
this machine's values; the file class decides which names it may use. A name the
materializer cannot fill never fails the install: the file is written with the
token left in place, a warning is printed, and `UPDATE_DEFERRED.md` records it
until a clean render clears it.

<!-- BEGIN: placeholder-table (generated from vco_lib/materialize.py REGISTRY) -->
| Placeholder | Expands to | Where it may appear |
|---|---|---|
| `{{ORCHESTRATOR_ROOT}}` | The orchestrator clone this install runs from | `VCO-REWIRE` regions, agents, skills, project templates |
| `{{PROJECT_ROOT}}` | The project folder being installed into (the orchestrator root on a self-install) | `VCO-REWIRE` regions, agents, skills, project templates |
| `{{PROJECTS_ROOT}}` | Parent of the orchestrator dir | `VCO-REWIRE` regions, agents, skills, project templates |
| `{{HOME}}` | Your home directory | `VCO-REWIRE` regions, agents, skills, project templates |
| `{{VCT_ORCHESTRATOR_ROOT}}` | The literal `${VCT_ORCHESTRATOR_ROOT}`, expanded by the consumer at run time | `VCO-REWIRE` regions, agents, skills, project templates |
| `{{PROJECT_NAME}}` | The project's registered name (launcher.db), else the name given at install, else the folder basename | agents, skills, project templates, `rendered_root_files.toml` entries |
| `{{VENV_PYTHON}}` | The install venv's interpreter (`.venv/bin/python`, `.venv\Scripts\python.exe` on Windows) | agents, skills, project templates, `rendered_root_files.toml` entries |
| `{{WEAVIATE_URL}}` | Weaviate HTTP URL from this machine's endpoint row | agents, skills, project templates, `rendered_root_files.toml` entries |
| `{{WEAVIATE_GRPC_PORT}}` | Weaviate gRPC port from this machine's endpoint row | agents, skills, project templates, `rendered_root_files.toml` entries |
| `{{OLLAMA_URL}}` | Ollama URL from this machine's endpoint row | agents, skills, project templates, `rendered_root_files.toml` entries |
| `{{CODE_EMBED_URL}}` | Code-embedding service URL from this machine's endpoint row | agents, skills, project templates, `rendered_root_files.toml` entries |
| `{{HUB_PORT}}` | The vct-hub port at render time (`$VCT_HUB_PORT` → `hub.port` → 7700); a snapshot — clients re-resolve that ladder at run time | agents, skills, project templates, `rendered_root_files.toml` entries |
| `{{INSTALLED_AT_PATH}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{WORKING_DIR}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{WRAPPER_SCRIPT}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{LOG_FILE}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{BOOT_LOG_FILE}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{LABEL}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{CREATED_AT}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{USER_ID}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{EXEC_START}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{EXEC_ARGV_PLIST}}` | boot unit (value from the unit spec, vco_lib/boot_service.py); a pre-escaped `<string>` fragment | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{EXEC_COMMAND}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{EXEC_ARGUMENTS}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{STATE_DIR}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
| `{{SECRET_PROJECT}}` | boot unit (value from the unit spec, vco_lib/boot_service.py) | boot-unit templates (`templates/systemd`, `launchd`, `windows`) |
<!-- END: placeholder-table -->

Keep placeholders in templates — do NOT hard-code paths, ports or URLs.

## Install flags

```bash
python install.py                      # 44 bundled agents + all skills (default)
python install.py --no-agents          # skip agent installation
python install.py --no-skills          # skip skill installation
```

Reinstalls preserve any agents/skills already present — you won't lose customizations.

## Adding your own agents

1. Drop `.md` files directly in `.claude/agents/` (not `templates/agents/`) — those are yours, not managed by install.py.
2. If you want your agent to be reinstalled on fresh installs, contribute it to `templates/agents/free/` in a PR.
