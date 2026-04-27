# User Journey

How a new user gets from "never heard of this" to "productive across 3 projects." Reference for landing page copy, onboarding emails, and docs/demo flows.

## 1. Discovery and install

```
User hears about VibeCoded Orchestrator (HN, Twitter, friend).
Visits vibecodedtools.it or GitHub repo.
Reads README → installs:

    git clone https://github.com/hotak92/vibecoded-orchestrator
    cd vibecoded-orchestrator
    python install.py
```

`install.py` detects the user's hardware (GPU/CPU/Apple Silicon), starts Weaviate + Ollama in containers, pulls embedding models, installs 19 free-tier agents and 28 skills (10 MAO-tier specialist agents are opt-in via `--with-mao-agents`), and writes `.env` and `.claude/settings.json`.

First-run time: ~5 min of interactive setup, plus 10–30 min for container image and model downloads (~5 GB for Weaviate + Ollama qwen3 weights; GPU mode adds CodeSage-Large-v2, another ~2.5 GB). On a fast link this lands closer to 10 min; on a slow connection budget for 30+. Re-installs reuse cached images and finish in seconds.

## 2. First orchestrator session

```
User opens the orchestrator project in any Claude Code surface — VS Code extension, the Claude Code CLI (`cd <project> && claude`), or the Claude Desktop app.

User: "What can you help me with?"

Claude: I'm running on the VibeCoded Orchestrator. I can help you:
        - Analyze any project and set up its Claude Code configuration
        - Build a knowledge graph of what you're working on
        - Search across all your projects semantically
        - Orchestrate agents to automate repetitive workflows

        Do you have a project you want to configure?
```

## 3. Configure a new project

```
User: "Set up my FastAPI project at ~/dev/my-api"

Claude:  *analyzes codebase via code graph + file scan*

         Detected:
         - Python 3.12, FastAPI, SQLAlchemy, Postgres
         - pytest, Docker
         - React frontend under ./web (Next.js 14)

         Recommended skills (7 total):
          - api-designer — REST endpoint patterns
          - database-advisor — SQL + SQLAlchemy
          - tdd — test-first workflow
          - security-reviewer — auth/injection coverage
          - performance-optimizer — query + render tuning
          - react-patterns — frontend state mgmt
          - doc-template — API + README docs

         I'll write:
          - ~/dev/my-api/.claude/settings.json (permissions + hooks)
          - ~/dev/my-api/.vscode/settings.json (MCP env — KG_COLLECTION=MyAPI)
          - ~/dev/my-api/CLAUDE.md (project instructions based on your stack)
          - ~/dev/my-api/.claude/CONTEXT_STATE.md (initial state)

         Proceed?

User: yes

Claude:  *writes files, triggers initial code graph analysis in background*
         Done. Open ~/dev/my-api in your editor (new VS Code window, a
         fresh `claude` session in that directory, or the Claude Desktop
         app) and Claude there will know your project.
```

## 4. Cross-project memory in action

A week later, the user is working on a *different* project and asks Claude a question. Because the orchestrator's knowledge graph is shared (`SHARED_KG_COLLECTION`), Claude pulls relevant patterns from the FastAPI project on its own.

```
User (in different project): "How should I structure auth for this microservice?"

Claude:  Based on your FastAPI project's auth middleware (Knowledge Graph node
         "jwt-middleware-pattern", last updated 2026-04-15), you're already using
         JWT + Pydantic verification. Want me to apply the same pattern here,
         or do you want a different approach for this service?
```

No re-explanation. No re-reading the old codebase. The knowledge is persistent.

## 5. Upgrade to Pro or MAO

As the user's project portfolio grows, RL retrieval reranking starts to matter — it learns which KG nodes the user actually acts on. They upgrade to Pro via the VCT Launcher.

Teams that want parallel agents working across a codebase upgrade to MAO and get 10 specialist agents plus the Tauri desktop UI for orchestration oversight.

## 6. Typical long-term usage

- Free tier users: 1-3 projects, moderate KG (~500-2000 nodes), casual use
- Pro users: 3-10 projects, RL-ranked retrieval improves hit rate by ~25-40% over raw semantic
- MAO users: 5+ projects, regular multi-agent runs, team coordination via coordination MCP
