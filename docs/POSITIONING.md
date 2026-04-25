# VibeCoded Orchestrator — Positioning & Target Market

Launch-asset source document. Use for Show HN drafts, landing page copy, and sales conversations.

## What makes this different

The orchestrator is infrastructure for Claude Code that solves three problems no other tool addresses together:

| Problem | How we solve it |
|---|---|
| **Context amnesia** | Knowledge Graph with semantic search — Claude remembers across sessions |
| **Code blindness** | Code graph indexes every function, class, API, cross-service call |
| **Workflow repetition** | 26 specialist agents + 29 skills auto-install, hooks automate repetitive ops |

All three are built on the same infrastructure (Weaviate + Ollama + Claude Code's MCP protocol). One install, three capabilities.

## Target market

**Primary**: developers and small teams using Claude Code for multiple projects

- Managing 3+ projects simultaneously
- Want consistent patterns and memory across codebases
- Freelancers and consultants reusing solutions across clients
- Dev teams adopting Claude Code at scale

**Secondary**: companies who need cross-project knowledge sharing, temporal tracking of architecture decisions, and code understanding without re-explaining context to every new session.

## Competitive positioning

The orchestrator category is young. The closest comparables:

**vs single-project codebase tools** (SummonAI Kit, GitHub Copilot Chat project indexing):
- They: index one project at a time, no cross-project memory, CLI-only setup
- Us: cross-project semantic search, persistent knowledge graph, conversational project setup via Claude, temporal reasoning over how decisions evolved

**vs raw Claude Code without orchestrator**:
- Without us: re-explain architecture every session, lose context between tasks, manually curate CLAUDE.md
- With us: Claude reads the KG on startup, auto-updates on file changes, suggests patterns from similar past work

**vs agent frameworks** (CrewAI, AutoGen, LangGraph):
- They: framework for building new agent systems from scratch
- Us: augment the agent system you're already using (Claude Code), no rewrite required

## Why now

- Claude Code adoption inflected in Q1 2026 — power users are hitting the context-limit ceiling and looking for solutions
- Claude Agent SDK / Agent Teams features turned on by default — multi-agent workflows went mainstream
- Local LLMs (Ollama + Qwen3) are good enough for embeddings that keep data private
- Enterprise interest in AI code tools is rising but needs cross-project memory guarantees

## The three-tier pitch

| Tier | For | Key feature |
|---|---|---|
| **Free** | Solo developers | Full orchestrator, knowledge graph, code graph, 26 agents, 29 skills |
| **Pro** | Developers with 3+ projects | Free + RL retrieval reranking (learns your preferences over time) + auto-updates |
| **MAO** | Teams + advanced users | Pro + full multi-agent orchestrator with 10 specialist agents + Tauri desktop UI + team coordination |

Free tier is AGPL. Pro and MAO are commercial subscriptions distributed as signed pre-compiled artifacts; subscribers get the binaries, not source.
