# Knowledge Graph Tag Hierarchy

**Version**: 1.0
**Created**: 2026-01-29
**Status**: Active
**Purpose**: Formal tag taxonomy for Claude Orchestrator knowledge graph

---

## Tag Organization Principles

1. **Consistent Format**: All tags start with `#`, lowercase except acronyms
2. **Multi-word Tags**: Use hyphens (e.g., `#low-level-implementation`)
3. **Tag Count**: 3-10 tags per node (minimum 3, optimal 5-7)
4. **Required Categories**: Every node should have:
   - At least 1 domain tag
   - At least 1 abstraction level tag (for technical content)
   - At least 1 technology or status tag

---

## Tag Hierarchy

### Level 1: Domain Tags (Top-Level Categories)

Primary domains for knowledge organization:

```
#AI (Artificial Intelligence)
├── #ML (Machine Learning)
│   ├── #LLM (Large Language Models)
│   ├── #VLM (Vision-Language Models)
│   ├── #embedding (Embedding models)
│   └── #training (Model training)
├── #NLP (Natural Language Processing)
│   ├── #text-generation
│   ├── #sentiment-analysis
│   └── #entity-extraction
└── #CV (Computer Vision)
    ├── #image-generation
    ├── #object-detection
    └── #face-recognition

#database
├── #vector-db (Vector databases)
├── #relational-db (Relational databases)
├── #graph-db (Graph databases)
└── #nosql (NoSQL databases)

#workflow
├── #automation
├── #orchestration
├── #agents
└── #hooks

#tooling
├── #development-tools
├── #testing
├── #debugging
└── #profiling

#infrastructure
├── #devops
├── #containerization
├── #deployment
└── #monitoring

#frontend
├── #react
├── #vue
├── #ui-components
└── #styling

#backend
├── #api
├── #microservices
├── #authentication
└── #data-processing

#security
├── #cryptography
├── #authorization
├── #vulnerability
└── #best-practices

#projects (Project-specific knowledge)
├── #claude-orchestrator (Meta-project for knowledge orchestration)
├── #project-x (one tag per project — replace with your own)
└── [Add project tags as needed for project-specific nodes]
```

**Project Tags Usage**:
- Use lowercase with hyphens for project names
- Add to nodes that are specific to a particular project
- Helps filter knowledge by project context
- Example: Tutorial for project X → Tag `#project-x` + domain tags
- Cross-project patterns should NOT use project tags (use domain tags only)

### Level 2: Abstraction Level Tags

Indicates knowledge depth and technical level:

```
#high-level-plan
  Purpose: Strategic overview, roadmap, architecture decisions
  Use for: Project overviews, long-term plans, architectural choices
  Example nodes: "Acme Vision", "Claude Orchestrator Roadmap"

#mid-level-architecture
  Purpose: System design, component interaction, integration patterns
  Use for: Architecture diagrams, design patterns, system workflows
  Example nodes: "MCP Server Architecture", "RAG Pipeline Design"

#low-level-implementation
  Purpose: Code-level details, specific APIs, implementation specifics
  Use for: Code examples, API references, specific algorithms
  Example nodes: "Weaviate Query Syntax", "Ollama API Integration"

#function-description
  Purpose: Individual function or method documentation
  Use for: Detailed function specifications, parameter descriptions
  Example nodes: "kg-search Implementation", "Chunking Algorithm"
```

### Level 3: Technology Tags

Specific technologies, frameworks, and tools:

**Programming Languages**:
- `#python`
- `#javascript`
- `#typescript`
- `#rust`
- `#go`
- `#bash`

**Frameworks**:
- `#fastapi`
- `#django`
- `#react`
- `#nextjs`
- `#vue`
- `#express`

**Databases**:
- `#weaviate`
- `#postgresql`
- `#mongodb`
- `#redis`
- `#neo4j`

**AI/ML Tools**:
- `#ollama`
- `#langchain`
- `#transformers`
- `#pytorch`
- `#tensorflow`

**Infrastructure**:
- `#docker`
- `#kubernetes`
- `#nginx`
- `#aws`
- `#gcp`

**Development Tools**:
- `#git`
- `#vscode`
- `#pytest`
- `#jest`

### Level 4: Status Tags

Lifecycle and work status:

```
#idea → #in-progress → #implemented → #tested → #deployed
                           ↓
                      #archived / #deprecated
```

**Status Tag Definitions**:

- **`#idea`**: Conceptual stage, not yet started
  - Use for: Brainstorming, future work, proposals
  - Typical next: `#in-progress` when work begins

- **`#in-progress`**: Active development or work
  - Use for: Current tasks, ongoing projects
  - Typical next: `#implemented` when complete

- **`#implemented`**: Completed but not yet tested
  - Use for: Finished code, deployed features (pre-testing)
  - Typical next: `#tested` after verification

- **`#tested`**: Verified and validated
  - Use for: Tested implementations, proven approaches
  - Typical next: `#deployed` if applicable, or stay as `#tested`

- **`#deployed`**: In production use
  - Use for: Live systems, active production code
  - Typical next: `#archived` when replaced

- **`#archived`**: No longer actively used but preserved
  - Use for: Old implementations, superseded approaches
  - Typical next: `#deprecated` if no longer recommended

- **`#deprecated`**: Actively discouraged, superseded
  - Use for: Obsolete patterns, outdated tools
  - Should link to replacement with `replacedBy::` relationship

### Level 5: Pattern Tags

Common design patterns and architectural patterns:

**AI Patterns**:
- `#RAG` (Retrieval Augmented Generation)
- `#fine-tuning`
- `#prompt-engineering`
- `#chain-of-thought`
- `#few-shot-learning`

**Architecture Patterns**:
- `#microservices`
- `#monolith`
- `#event-driven`
- `#layered-architecture`
- `#hexagonal`

**Data Patterns**:
- `#caching`
- `#batching`
- `#streaming`
- `#pagination`
- `#denormalization`

**Integration Patterns**:
- `#MCP` (Model Context Protocol)
- `#REST-API`
- `#GraphQL`
- `#webhooks`
- `#message-queue`

---

## Tag Combination Patterns

### By Node Type

**Project Nodes** should typically have:
- 1 domain tag (`#AI`, `#database`, etc.)
- 1 abstraction tag (`#mid-level-architecture` or `#high-level-plan`)
- 2-3 technology tags (stack used)
- 1 status tag (`#in-progress`, `#implemented`, etc.)
- 1 project tag (`#project-x`, `#claude-orchestrator`, etc.)
- Optional: Pattern tags for architectural patterns used

**Example**: Acme Project
```
Tags: [#AI, #RAG, #conversational-AI, #implemented, #mid-level-architecture,
       #fastapi, #weaviate, #ollama, #project-x]
```

**Concept Nodes** should typically have:
- 1 domain tag
- 1 abstraction tag
- 1-2 related technology or pattern tags
- No status tag (concepts are timeless)

**Example**: VRAM Optimization
```
Tags: [#AI, #ML, #performance, #optimization, #mid-level-architecture,
       #quantization, #memory-management]
```

**Tool Nodes** should typically have:
- 1 domain tag
- Technology-specific tags
- Capability tags (what it enables)
- No abstraction level tag (tools are concrete)

**Example**: Weaviate
```
Tags: [#database, #vector-db, #semantic-search, #AI, #weaviate,
       #graph-db, #hybrid-search]
```

**Research Nodes** should typically have:
- 1-2 domain tags
- Methodology tags
- No status tag
- Link to findings and references

**Example**: VLM Comparison 2026
```
Tags: [#AI, #VLM, #research, #benchmark, #document-understanding,
       #qwen, #pixtral, #evaluation]
```

**Project-Specific Knowledge** (tutorials, setup guides, project docs):
- 1-2 domain tags
- 1 project tag (required for project-specific content)
- Technology tags for tools used
- Status tag if applicable (for guides that may become outdated)
- No abstraction level tag (project-specific = concrete)

**Example**: Acme Weaviate Setup Guide
```
Tags: [#database, #vector-db, #weaviate, #project-x, #setup, #low-level-implementation]
```

**Example**: Claude Orchestrator RDF Improvements Plan
```
Tags: [#workflow, #knowledge-graph, #claude-orchestrator, #idea, #mid-level-architecture]
```

**When to use project tags**:
- ✅ Setup guides specific to a project
- ✅ Project-specific configuration files
- ✅ Lessons learned from a specific project
- ✅ Project roadmaps and plans
- ❌ General patterns that apply across projects (use domain tags only)
- ❌ Tools/concepts that aren't project-specific

---

## Tag Inference Rules

These rules are used by the automated inference engine to derive tags from relationships:

### Rule 1: Domain Tag Propagation

When a node has a typed relationship to another node:
- **Uses/Implements relationship** → Inherit domain tags from target
  - Project [[uses::Weaviate]] → Inherits `#database`, `#vector-db`
  - Concept [[implements::RAG]] → Inherits `#AI`, `#retrieval`

### Rule 2: Capability Tag Inheritance

When a node uses a tool:
- **Uses relationship** → Inherit capability tags (hyphenated tags)
  - Project [[uses::Weaviate]] → Inherits `#vector-search`, `#semantic-search`
  - Project [[uses::FastAPI]] → Inherits `#async`, `#type-hints`

### Rule 3: Pattern Tag Propagation

When a node implements a pattern:
- **Implements relationship** → Inherit pattern tag
  - Project [[implements::RAG Pattern]] → Gets `#RAG`
  - Tool [[implements::MCP Pattern]] → Gets `#MCP`

### Rule 4: Technology Stack Inference

When multiple related tools are used:
- Detect common stacks and add meta-tag
  - Uses [FastAPI, Pydantic, Uvicorn] → Add `#python-stack`
  - Uses [React, NextJS, TypeScript] → Add `#react-stack`

---

## Tag Consistency Guidelines

### DO ✅

- Use lowercase for general tags: `#api`, `#database`, `#optimization`
- Use UPPERCASE for acronyms: `#AI`, `#ML`, `#NLP`, `#RAG`, `#MCP`
- Use hyphens for multi-word: `#low-level-implementation`, `#vector-db`
- Use specific technology names: `#weaviate`, `#fastapi`, `#ollama`
- Include abstraction level for technical content
- Update tags when status changes (idea → in-progress → implemented)

### DON'T ❌

- Don't use spaces: `#low level` ❌ → `#low-level` ✅
- Don't use CamelCase: `#VectorDatabase` ❌ → `#vector-db` ✅
- Don't use underscores: `#low_level` ❌ → `#low-level` ✅
- Don't over-tag: 15+ tags is too many, keep it focused
- Don't under-tag: <3 tags makes discovery hard
- Don't duplicate meaning: `#database` + `#db` ❌ → pick one ✅

---

## Tag Migration Guide

### Converting Old Tags to New Hierarchy

| Old Tag | New Tag(s) | Reason |
|---------|-----------|--------|
| `#test` | `#in-progress` or `#testing` | More specific status |
| `#project` | `#<project-name>` + domain tags | Add context |
| `#wip` | `#in-progress` | Standardized status |
| `#done` | `#implemented` or `#tested` | More precise |
| `#coding` | `#low-level-implementation` | Abstraction level |
| `#design` | `#mid-level-architecture` | Abstraction level |
| `#ai-tool` | `#AI` + `#tool` + specific tool | More specific |
| `#research_notes` | `#research` + domain | Consistent format |

### Adding Missing Tags

For nodes with insufficient tags:

1. **Identify domain**: What field does this belong to?
   - Add: `#AI`, `#database`, `#workflow`, etc.

2. **Determine abstraction level**: How technical is this?
   - Add: `#high-level-plan`, `#mid-level-architecture`, or `#low-level-implementation`

3. **Check status**: What's the lifecycle stage?
   - Add: `#idea`, `#in-progress`, `#implemented`, etc.

4. **Add technologies**: What specific tools/frameworks?
   - Add: `#weaviate`, `#fastapi`, `#python`, etc.

5. **Identify patterns**: Any design patterns used?
   - Add: `#RAG`, `#MCP`, `#microservices`, etc.

---

## Validation Checklist

When reviewing node tags, ensure:

- [ ] 3-10 tags total (optimal: 5-7)
- [ ] At least 1 domain tag (`#AI`, `#database`, etc.)
- [ ] At least 1 abstraction level tag (for technical nodes)
- [ ] All tags are lowercase (except acronyms)
- [ ] Multi-word tags use hyphens
- [ ] No duplicate/overlapping tags
- [ ] Tags match relationships (e.g., if [[uses::Weaviate]], has `#weaviate`)
- [ ] Status tag is current (matches actual state)

---

## Examples

### Well-Tagged Nodes

```markdown
# Acme Project
Tags: [#AI, #RAG, #conversational-AI, #implemented, #mid-level-architecture,
       #fastapi, #weaviate, #ollama, #MCP]
✅ Good: Domain (AI), abstraction (mid-level), status (implemented), technologies (fastapi, weaviate, ollama), pattern (RAG, MCP)
```

```markdown
# VRAM Optimization
Tags: [#AI, #ML, #performance, #optimization, #mid-level-architecture,
       #quantization, #memory-management]
✅ Good: Domain (AI, ML), abstraction (mid-level), focus area (performance), specific techniques (quantization)
```

```markdown
# Weaviate Query Syntax
Tags: [#database, #vector-db, #weaviate, #low-level-implementation,
       #query-language, #API]
✅ Good: Domain (database), technology (weaviate), abstraction (low-level), specific area (query-language)
```

### Poorly-Tagged Nodes (Before Improvement)

```markdown
# Some Project
Tags: [#project, #test, #wip]
❌ Bad: Generic tags, no domain, no technologies, unclear status
Should be: [#AI, #RAG, #in-progress, #mid-level-architecture, #fastapi, #weaviate]
```

```markdown
# Database Setup
Tags: [#database]
❌ Bad: Only 1 tag, missing context
Should be: [#database, #postgresql, #setup, #configuration, #low-level-implementation, #docker]
```

---

## Future Extensions

Planned tag categories for future expansion:

- **Performance tags**: `#latency`, `#throughput`, `#scalability`
- **Quality tags**: `#tested`, `#reviewed`, `#documented`, `#optimized`
- **Complexity tags**: `#simple`, `#moderate`, `#complex`, `#expert-level`
- **License tags**: `#open-source`, `#commercial`, `#MIT`, `#Apache`
- **Platform tags**: `#linux`, `#windows`, `#macos`, `#cross-platform`

---

## References

- [SKOS: Simple Knowledge Organization System](https://www.w3.org/2004/02/skos/)
- [Folksonomy vs Taxonomy](https://en.wikipedia.org/wiki/Folksonomy)
- [Tag Guidelines (Stack Overflow)](https://stackoverflow.com/help/tagging)
