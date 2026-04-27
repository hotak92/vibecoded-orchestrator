# VibeCoded Orchestrator — Positioning & Target Market

Source doc for launch assets — Show HN draft, landing page copy, sales conversations.

## What makes this different

The orchestrator is infrastructure for Claude Code. Three problems it addresses that no other tool covers together:

| Problem | What we add |
|---|---|
| Context amnesia between sessions | Knowledge Graph with semantic search — Claude reads it on session start |
| Code blindness | Code graph indexes every function, class, API, and cross-service call |
| Repetitive setup and ops | 19 specialist agents + 28 skills installed into `.claude/`; 20 hooks handle the rest |

All three sit on the same infrastructure (Weaviate + Ollama + Claude Code's MCP protocol). One install, three capabilities.

## Target market

**Primary**: developers and small teams using Claude Code on multiple projects.

- Managing 3+ projects simultaneously
- Want consistent patterns and memory across codebases
- Freelancers and consultants reusing solutions across clients
- Dev teams adopting Claude Code at scale

**Secondary**: companies that need cross-project knowledge sharing, temporal tracking of architecture decisions, and code understanding without re-explaining context to every new session.

## Competitive positioning

The orchestrator category is young. Closest comparables:

**vs single-project codebase tools** (SummonAI Kit, GitHub Copilot Chat project indexing):
- They: index one project at a time, no cross-project memory, CLI-only setup
- Us: cross-project semantic search, persistent knowledge graph, conversational project setup via Claude, temporal reasoning over how decisions evolved

**vs raw Claude Code without orchestrator**:
- Without us: re-explain architecture every session, lose context between tasks, hand-curate CLAUDE.md
- With us: Claude reads the KG on startup, auto-updates on file changes, surfaces patterns from similar past work

**vs agent frameworks** (CrewAI, AutoGen, LangGraph):
- They: a framework for building new agent systems from scratch
- Us: augment the agent system you're already using (Claude Code), no rewrite required

## Why now

- Claude Code adoption inflected in Q1 2026. Power users are hitting the context-limit ceiling and looking for ways out.
- Claude Agent SDK and Agent Teams shipped on-by-default — multi-agent workflows went mainstream.
- Local LLMs (Ollama + Qwen3) are now good enough for embeddings that keep data private.
- Enterprise interest in AI coding tools is rising, but adoption needs cross-project memory guarantees that don't exist elsewhere.

## The three-tier pitch

| Tier | For | Key feature |
|---|---|---|
| **Free** | Solo developers | Full orchestrator, knowledge graph, code graph, 19 agents, 28 skills |
| **Pro** | Developers with 3+ projects | Free + RL retrieval reranking (learns your preferences over time) + auto-updates |
| **MAO** | Teams + advanced users | Pro + full multi-agent orchestrator with 10 specialist agents + Tauri desktop UI + team coordination |

Free tier is AGPL. Pro and MAO are commercial subscriptions distributed as signed pre-compiled artifacts; subscribers get the binaries, not source.
