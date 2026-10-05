# Templates — Agents, Skills, Packs, Specialisations

This directory holds what `install.py` / `python -m vco_lib.project_init install-bundle` copies into a user's `.claude/` at install time: default agents (`agents/free/`), module-gated gateway agents (`agents/module-gateway/`), default skills (`skills/`), opt-in packs (`packs/`), and specialisation reference docs (`specializations/`). Contents here are *templates*, not active definitions — they're populated into your project's `.claude/` so Claude Code picks them up.

## Default agents — `agents/free/` (11 agents)

Installed by default in every project. ONE definition per agent: the dispatcher may run any of them on a different Claude tier via the Agent-tool `model` parameter (each body names its sanctioned alternates in a "Model guidance" line). No agent carries a `skills:` frontmatter block — specialisation depth lives in `specializations/` docs referenced by path.

| Agent | Model | Role |
|---|---|---|
| `expert-coder` | opus | Implementation and debugging: features, refactors, fixes, backend/API work |
| `frontend-specialist` | sonnet | React/Vue/Svelte UI implementation with a11y + real async states |
| `code-explorer` | haiku | Read-heavy codebase research that saves its report to disk |
| `web-explorer` | haiku | Web/local-docs research (SURVEY + DEEP modes) saved as a report |
| `planner` | opus | Requirements → architecture → phased, testable implementation plans |
| `tester` | sonnet | Writes/runs tests, root-causes failures, reviews coverage |
| `gui-tester` | sonnet | Playwright-driven GUI testing with screenshots + structured report |
| `doc-maintainer` | sonnet | Doc extraction, organization, and the archive-before-forget pipeline |
| `kg-maintainer` | sonnet | KG search, node writing, duplicate + health repair |
| `code-reviewer` | fable | Adversarial read-only review; findings with `file:line` evidence |
| `agent-author` | sonnet | Agent/skill definitions and helper scripts |

## Gateway agents — `agents/module-gateway/` (8 agents)

Delivered ONLY when the model gateway is configured on the machine (tri-state module gate); they pin `claude-gw/*` model ids in frontmatter and are dispatched by name with no model override. The z.ai lane: `glm-implementer`, `glm-reviewer`, `glm-planner`, `glm-flash-researcher`. The qwen-vendor lane: `deepseek-implementer`, `qwen-implementer`, `deepseek-researcher`, `qwen-flash-sweeper`. There is deliberately no reviewer and no planner on the qwen vendor.

## Default skills — `skills/` (6 skills)

Short-form guidance invoked via `/skill-name` or auto-loaded by description match:

| Skill | Role |
|---|---|
| `context-compress` | /compact pipeline guidance + CONTEXT_STATE.md inspection/maintenance |
| `fix-issue` | Structured GitHub-issue/bug investigation → fix + regression test |
| `orchestrator-installer` | Diagnose partially-failed VCO installs; install.py flag advice |
| `project-bootstrapper` | Human-led second pass on a project's seeded bootstrap docs |
| `rc-native` | Remote Control as a detached native-auth server alongside the gateway panel |
| `task-breakdown` | Feature → 1–2 h tasks with estimates, dependency graph, risks |

## Opt-in packs — `packs/` (11 packs: 19 agents + 30 skills)

NOT installed by default. `packs/packs.toml` is the ONE committed table defining every pack and its members (parsed by `vco_lib/packs.py`). Install into a project with `python -m vco_lib.project_init install-bundle --folder <project> --update --pack <name>` (or the launcher's Packs tab); remove with `--remove-pack <name>` (user-modified members are backed up before removal, never silently deleted). Once installed, a pack stays current through ordinary bundle updates.

| Pack | Agents | Skills |
|---|---|---|
| `dev-advisors` | — | accessibility-checker, ai-rag-advisor, architect, debug-expert, security-reviewer |
| `devops-reliability` | — | idempotency-keys, k8s-manifest-reviewer, slo-designer, terraform-plan-reviewer, webhook-receiver |
| `ai-engineering` | ai-llm-expert, ai-agentic-architect | structured-output-extraction, workflow-cost-estimator |
| `science` | discipline-expert, experiment-designer, paper-triage | equation-check, hpc-submit, repro-audit, stats-consult |
| `marketing-sales-product` | — | build-vs-buy-decision, content-calendar-planner, saas-metrics-health-check, saas-pricing-strategist, sales-call-prep, seo-content-brief |
| `consulting` | consulting-cto-portfolio-coordinator, consulting-employee-impersonator, consulting-sow-drafter | consulting-due-diligence, consulting-incident-coordinator, consulting-portfolio-status |
| `design-media` | — | ai-image-prompting, batch-image-pipeline, design-system-auditor, gui-ux-expert, photoshop-scripting |
| `design-ux` | gui-expert, enterprise-ux-architect | — |
| `gtm-marketing` | brand-identity-architect, landing-page-critic, outbound-sequence-writer, inbox-triage-operator, launch-orchestrator | — |
| `ops-sre` | sre-incident-responder, postmortem-author, automation-engineer | — |
| `migration` | code-migrator | — |

Pack members install to the SAME locations as default agents/skills (`.claude/agents/<name>.md`, `.claude/skills/<name>/`), so enable/disable, adoption, and update semantics are identical.

## Specialisation docs — `specializations/` (17 docs)

Plain reference docs installed to `<project>/.claude/specializations/` — the depth that used to bloat agent prompts, now referenced by ONE line in the agent/skill body that needs it:

- `fields/` — backend, api-design, database, deployment, frontend, prompt-engineering
- `review-kinds/` — code, security, test, architecture-design, docs-vs-code (read by `code-reviewer` per review kind)
- `review-topics/` — performance, frontend-ui-a11y, database-migrations, api-design, infra-ci, data-ml
- `languages/` — ships only when a doc exists (no empty placeholders); agents reference it conditionally

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
| `{{MODEL_SELECTION_GRID}}` | The per-task model-selection table, rendered with only the rows for the model providers reachable on this machine at render time (a snapshot; no monitoring afterwards) | agents, skills, project templates, `rendered_root_files.toml` entries |
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
python install.py                      # 11 default agents + 6 default skills (+ 8 gateway agents when the model gateway is configured)
python install.py --no-agents          # skip agent installation
python install.py --no-skills          # skip skill installation
```

Per-project bundles (and opt-in packs) go through `python -m vco_lib.project_init install-bundle --folder <project> [--update] [--pack <name>] [--remove-pack <name>] [--skip-kind agents|skills|specializations]`.

Reinstalls preserve any agents/skills already present — you won't lose customizations.

## Adding your own agents

1. Drop `.md` files directly in `.claude/agents/` (not `templates/agents/`) — those are yours, not managed by install.py.
2. If you want your agent to be reinstalled on fresh installs, contribute it to `templates/agents/free/` in a PR.
