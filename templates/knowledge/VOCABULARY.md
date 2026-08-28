# Knowledge Graph Vocabulary

**Version**: 1.0
**Created**: 2026-01-29
**Status**: Active
**Purpose**: Formal vocabulary for Claude Orchestrator knowledge graph (RDF-inspired)

---

## Namespaces

Standard namespaces used in this knowledge graph:

| Prefix | Namespace | Description |
|--------|-----------|-------------|
| `co` | `claude-orch://knowledge/` | Claude Orchestrator internal vocabulary |
| `rdf` | `http://www.w3.org/1999/02/22-rdf-syntax-ns#` | RDF core vocabulary |
| `rdfs` | `http://www.w3.org/2000/01/rdf-schema#` | RDF Schema |
| `owl` | `http://www.w3.org/2002/07/owl#` | Web Ontology Language |
| `dc` | `http://purl.org/dc/elements/1.1/` | Dublin Core metadata |
| `skos` | `http://www.w3.org/2004/02/skos/core#` | Simple Knowledge Organization System |
| `dbp` | `http://dbpedia.org/resource/` | DBpedia resources |

---

## Classes (Node Types)

### Core Classes

#### **`co:Project`** (alias: `project`)
- **Definition**: A software project, implementation, or initiative
- **Parent**: `rdfs:Resource`
- **Properties**: title, status, tech_stack, repository, created, updated
- **Examples**: Acme Project, Claude Orchestrator, Example WebApp

#### **`co:Concept`** (alias: `concept`)
- **Definition**: Abstract idea, design pattern, or theoretical knowledge
- **Parent**: `skos:Concept`
- **Properties**: title, definition, related_concepts, examples
- **Examples**: VRAM Optimization, RAG Strategies, Agent Architecture

#### **`co:Tool`** (alias: `tool`)
- **Definition**: Software tool, library, framework, or technology
- **Parent**: `rdfs:Resource`
- **Properties**: title, version, documentation, capabilities, license
- **Examples**: Weaviate, FastAPI, Ollama, Docker

#### **`co:Model`** (alias: `model`)
- **Definition**: AI/ML model (LLM, VLM, embedding model, etc.)
- **Parent**: `co:Tool`
- **Properties**: title, architecture, parameters, vram_requirements, quantization
- **Examples**: Llama-3.1-70B, Qwen2.5-VL-7B, snowflake-arctic-embed2

#### **`co:Hardware`** (alias: `hardware`)
- **Definition**: Hardware specifications, constraints, or benchmarks
- **Parent**: `rdfs:Resource`
- **Properties**: title, specs, benchmarks, limitations
- **Examples**: RTX 4080 Super 16GB, VRAM Constraints

#### **`co:Research`** (alias: `research`)
- **Definition**: Research findings, papers, or experimental results
- **Parent**: `rdfs:Resource`
- **Properties**: title, methodology, findings, references, date
- **Examples**: VLM Comparison, RAG Evaluation 2026

#### **`co:Pattern`** (alias: `pattern`)
- **Definition**: Reusable implementation pattern or solution template
- **Parent**: `skos:Concept`
- **Properties**: title, problem, solution, tradeoffs, examples
- **Examples**: MCP Server Architecture, Conversation Memory Pattern

#### **`co:Insight`** (alias: `insight`)
- **Definition**: Lesson learned, gotcha, or practical wisdom
- **Parent**: `rdfs:Resource`
- **Properties**: title, context, takeaway, references
- **Examples**: Weaviate RFC3339 Date Requirements, Async Hook Pitfalls

#### **`co:Guide`** (alias: `guide`)
- **Definition**: Step-by-step tutorial, how-to documentation, or comprehensive instructional material
- **Parent**: `rdfs:Resource`
- **Properties**: title, prerequisites, steps, examples, external_links
- **Examples**: SD1.5 LoRA Training Guide, ComfyUI Workflows, ControlNet Techniques

### Type Hierarchy

```
rdfs:Resource
├── co:Project
├── co:Tool
│   └── co:Model
├── co:Hardware
├── co:Research
├── co:Insight
├── co:Guide
└── skos:Concept
    ├── co:Concept
    └── co:Pattern
```

### Declaring your own node types

The node-type vocabulary is **open**: the orchestrator's validators and
path normalization read this file, so adding a class section here extends
the accepted `type:` values for this project.

**Prefer an existing type.** Reuse a built-in (or an already-declared
custom type) whenever a suitable one exists — `concept` and `insight`
cover most knowledge. Add a new type only when it is genuinely necessary
for retrieval or organization.

To declare one, add a class section following the exact heading shape the
built-ins above use — a heading with the bold `co:` name and an
`(alias: `…`)` — plus an optional **Folder** bullet:

```markdown
#### **`co:Thought`** (alias: `thought`)
- **Definition**: A fleeting idea captured before it is lost
- **Folder**: `thoughts`
- **Properties**: title, context, next_step
```

Rules the tooling applies:

- **Only real heading lines count.** Mentions of `alias:` in prose,
  tables, or fenced code blocks (like the example above) declare nothing.
- The **alias** (lowercased) becomes the `type:` value nodes may use.
- The optional `- **Folder**: `name`` bullet gives the type a dedicated
  `knowledge/<name>/` subfolder: nodes of that type are auto-filed there,
  and paths under that subfolder are trusted by path normalization. The
  value must be a single path segment (letters, digits, `_`, `-`).
- **Without a Folder line** a custom type files under `knowledge/concepts/`
  — the same default the built-in `pattern` / `insight` / `guide` types use.
- A declaration cannot re-route a **built-in** type to a different folder;
  built-in routing always wins.
- **Capacity note**: keep the total type set modest. Past 256 types the
  parser emits a soft warning — the RL reranker's type-embedding registry
  is sized in the RL module itself, so verify against your module's
  capacity before relying on RL reranking across that many categories.

---

## Properties (Typed Relationships)

### Core Relationships

#### **`uses`** (co:uses)
- **Domain**: co:Project, co:Concept
- **Range**: co:Tool, co:Model
- **Definition**: Entity utilizes or depends on the target resource
- **Inverse**: `usedBy`
- **Examples**:
  - Acme Project [[uses::Weaviate]]
  - VRAM Optimization [[uses::Model Quantization]]

#### **`implements`** (co:implements)
- **Domain**: co:Project, co:Tool
- **Range**: co:Concept, co:Pattern
- **Definition**: Entity implements or realizes the target concept/pattern
- **Inverse**: `implementedBy`
- **Examples**:
  - Acme [[implements::RAG Pattern]]
  - MCP Server [[implements::Tool Use Pattern]]

#### **`extends`** (rdfs:subClassOf)
- **Domain**: co:Concept, co:Pattern
- **Range**: co:Concept, co:Pattern
- **Definition**: Entity is a specialization or subtype of the target
- **Inverse**: `extendedBy`
- **Examples**:
  - GraphRAG [[extends::RAG]]
  - VRAM Optimization [[extends::Performance Optimization]]

#### **`buildsOn`** (co:buildsOn)
- **Domain**: co:Project, co:Concept
- **Range**: co:Project, co:Concept, co:Research
- **Definition**: Entity is built upon or derived from target work
- **Inverse**: `supports`
- **Examples**:
  - Claude Orchestrator [[buildsOn::Previous Workflow Attempts]]
  - Hybrid Search [[buildsOn::Semantic Search]]

#### **`relatedTo`** (skos:related)
- **Domain**: Any
- **Range**: Any
- **Definition**: Generic relationship for loosely connected entities
- **Symmetric**: Yes
- **Examples**:
  - VRAM Management [[relatedTo::Memory Pooling]]
  - Docker [[relatedTo::Kubernetes]]

#### **`partOf`** (dc:partOf)
- **Domain**: Any
- **Range**: co:Project, co:Concept
- **Definition**: Entity is a component or sub-part of the target
- **Inverse**: `hasPart`
- **Examples**:
  - MCP Server [[partOf::Claude Orchestrator]]
  - Chunking Strategy [[partOf::RAG Pipeline]]

#### **`dependsOn`** (co:dependsOn)
- **Domain**: co:Project, co:Tool
- **Range**: co:Tool, co:Hardware
- **Definition**: Entity requires the target for functionality
- **Inverse**: `requiredBy`
- **Examples**:
  - Llama-70B [[dependsOn::RTX 4090 48GB]]
  - Weaviate MCP [[dependsOn::Weaviate]]

#### **`sameAs`** (owl:sameAs)
- **Domain**: Any
- **Range**: Any (usually external resources)
- **Definition**: Entity is equivalent to the target (for linking to DBpedia, etc.)
- **Symmetric**: Yes
- **Examples**:
  - Weaviate [[sameAs::dbp:Weaviate]]
  - RDF [[sameAs::dbp:Resource_Description_Framework]]

### Property Hierarchy

```
Relationships:
├── uses (functional dependency)
├── implements (realization of concept)
├── extends (specialization, inheritance)
├── buildsOn (derivation, builds upon)
├── relatedTo (general association)
├── partOf (composition)
├── dependsOn (hard requirement)
└── sameAs (equivalence)
```

---

## Metadata Properties

Standard metadata fields for all nodes (from Dublin Core + custom):

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `dc:title` | string | Yes | Human-readable title |
| `rdf:type` | class | Yes | Node type (project, concept, tool, etc.) |
| `dc:created` | date | Yes | Creation timestamp |
| `dc:modified` | date | Yes | Last modification timestamp |
| `co:valid_from` | date | No | When knowledge became valid/relevant |
| `co:valid_until` | date | No | When knowledge expired/deprecated (null = still valid) |
| `co:status` | enum | Yes | active, archived, deprecated, idea |
| `dc:creator` | string | No | Author or source |
| `dc:description` | text | No | Brief description |
| `skos:prefLabel` | string | No | Preferred label for display |
| `skos:altLabel` | string[] | No | Alternative labels or aliases |

---

## Tag Vocabulary

### Domain Tags (High-Level Categories)

Tags that categorize by domain or discipline:

- `#AI` - Artificial Intelligence
- `#ML` - Machine Learning
- `#NLP` - Natural Language Processing
- `#CV` - Computer Vision
- `#database` - Database systems
- `#workflow` - Workflow automation
- `#tooling` - Development tools
- `#infrastructure` - Infrastructure and DevOps
- `#frontend` - Frontend development
- `#backend` - Backend development
- `#security` - Security and cryptography

### Abstraction Level Tags

Tags indicating knowledge abstraction level:

- `#high-level-plan` - Strategic overview, roadmap
- `#mid-level-architecture` - System design, component interaction
- `#low-level-implementation` - Code-level details, specific APIs

### Technology Tags

Tags for specific technologies (lowercase preferred):

- `#python` - Python language
- `#javascript` - JavaScript language
- `#react` - React framework
- `#fastapi` - FastAPI framework
- `#docker` - Docker containerization
- `#weaviate` - Weaviate vector database
- `#ollama` - Ollama local LLM server

### Status Tags

Tags indicating work status:

- `#idea` - Conceptual stage
- `#in-progress` - Active work
- `#implemented` - Completed implementation
- `#tested` - Tested and verified
- `#archived` - No longer active

### Pattern Tags

Tags for common patterns:

- `#RAG` - Retrieval Augmented Generation
- `#MCP` - Model Context Protocol
- `#agentic` - Agentic workflows
- `#vector-search` - Vector similarity search
- `#semantic-similarity` - Semantic similarity matching

### Project Tags

Tags for project-specific knowledge (lowercase with hyphens):

- `#claude-orchestrator` - Meta-project for knowledge orchestration
- `#project-x` - One tag per project (replace with your own)
- [Add more project tags as needed]

**Usage Guidelines**:
- Use for project-specific tutorials, setup guides, configurations
- Use for project roadmaps and plans
- Do NOT use for general patterns that apply across projects
- Keep lowercase with hyphens (e.g., `#my-project`, not `#MyProject`)

---

## Validation Rules

### Node Type Validation

1. **Required fields**:
   - `title`: Must be present and non-empty
   - `type`: Must be one of defined classes (project, concept, tool, model, hardware, research, pattern, insight, guide)
   - `created`, `updated`: Must be valid ISO 8601 dates
   - `status`: Must be one of {active, archived, deprecated, idea}

2. **Type-specific requirements**:
   - `project`: Should have `uses` and/or `implements` relationships
   - `concept`: Should have `extends` or `relatedTo` relationships
   - `tool`: Should have version info and documentation links
   - `model`: Should have VRAM requirements and architecture details
   - `guide`: Should have external_links for references and structured instructional content

### Relationship Validation

1. **Allowed relationships by type**:
   - Projects: uses, implements, buildsOn, dependsOn
   - Concepts: extends, relatedTo, buildsOn
   - Tools: uses, implements (patterns), dependsOn
   - Models: extends (other models), dependsOn (hardware)

2. **Cardinality**:
   - `sameAs`: Max 1 per external namespace
   - `extends`: Max 1 (single inheritance)
   - Others: Unlimited

3. **Target validation**:
   - `uses` → must point to tool or model
   - `implements` → must point to concept or pattern
   - `extends` → must point to same or parent type
   - `sameAs` → must be external URI (dbp:, wikipedia:, etc.)

### Tag Validation

1. **Format**:
   - Must start with `#`
   - Lowercase preferred (except acronyms: AI, ML, RAG)
   - Hyphens for multi-word tags
   - No spaces

2. **Consistency**:
   - Each node should have 3-10 tags
   - At least one domain tag
   - At least one abstraction level tag (if applicable)

---

## Usage Examples

### Project Node Example

```markdown
---
title: Acme Project
type: project
tags: [AI, RAG, conversational-AI, implemented, mid-level-architecture]
created: 2025-12-15T00:00:00Z
updated: 2026-01-20T14:30:00Z
status: active
external_links:
  github: https://github.com/user/acme
  official: https://acme.example.com
---

#AI #RAG #conversational-AI

Acme is a conversational AI assistant for a domain-specific knowledge base.

## Technology Stack

- **Backend**: [[uses::FastAPI]]
- **Database**: [[uses::Weaviate]]
- **LLM**: [[uses::Ollama]]

## Implemented Patterns

- [[implements::RAG Pattern]]
- [[implements::Conversation Memory Pattern]]
- [[implements::MCP Server Architecture]]
```

### Concept Node Example

```markdown
---
title: VRAM Optimization
type: concept
tags: [AI, ML, performance, optimization, mid-level-architecture]
created: 2025-11-20T00:00:00Z
updated: 2026-01-15T10:00:00Z
status: active
external_links:
  dbpedia: https://dbpedia.org/page/Graphics_processing_unit
---

#AI #ML #performance

VRAM Optimization techniques for running large language models.

## Parent Concept

[[extends::Performance Optimization]]

## Implementation Techniques

- [[uses::Model Quantization]]
- [[uses::Memory Pooling]]
- [[uses::Gradient Checkpointing]]
```

---

## Migration Checklist

When converting existing nodes to follow this vocabulary:

- [ ] Add `type` field to frontmatter (map current types to vocabulary)
- [ ] Convert untyped `[[links]]` to typed relationships where applicable
- [ ] Add domain tags (AI, database, workflow, etc.)
- [ ] Add abstraction level tags (high-level, mid-level, low-level)
- [ ] Add `status` field (active, archived, deprecated, idea)
- [ ] Add `external_links` for major tools/concepts (DBpedia, official docs)
- [ ] Verify relationships match allowed relationships for type
- [ ] Check tag formatting (`#lowercase-with-hyphens`)

---

## References

- [RDF 1.1 Primer](https://www.w3.org/TR/rdf11-primer/)
- [SKOS Simple Knowledge Organization System](https://www.w3.org/2004/02/skos/)
- [Dublin Core Metadata Terms](https://www.dublincore.org/specifications/dublin-core/dcmi-terms/)
- [OWL 2 Web Ontology Language](https://www.w3.org/TR/owl2-overview/)
