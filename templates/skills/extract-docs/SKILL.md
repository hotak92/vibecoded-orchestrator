---
name: extract-docs
description: Systematically extract knowledge from scattered documentation to prevent catastrophic forgetting. Creates structured extraction reports with status tags.
short_desc: extract knowledge from scattered docs (structured)
keywords: [documentation extraction, catastrophic forgetting, status tags, scattered docs, before archiving, "scattered documentation", "knowledge extraction", "before archival", "extract knowledge from docs", "pull info from documentation"]
argument-hint: "[source-path-or-pattern]"
tools: Read, Write, Grep, Glob, Bash, Task
model: sonnet
---

# Extract Documentation Knowledge (Sonnet)

**Purpose**: Systematically extract knowledge from scattered documentation to prevent catastrophic forgetting.

## When to Invoke Autonomously

1. **Project has 20+ scattered documentation files** - Risk of losing context
2. **Before archiving completed work** - Extract still-relevant content first
3. **Preparing canonical living documents** - Consolidate before creating
4. **After major phase completion** - Capture learnings before forgetting

## DO NOT invoke for

- Well-organized documentation (no consolidation needed)
- Active editing sessions (extract afterward)
- Single-file documentation tasks

## Search Systems

**1. kg-search/kg-info** (Keyword/Metadata) - Fast (~100ms):
- `.claude/scripts/kg-search search "term" [--type TYPE] [--tags TAGS]`
- `.claude/scripts/kg-info info "Title"`

**2. Weaviate MCP Tools** (Semantic/Graph):
- `search_knowledge_graph` - Basic semantic (~500ms)
- `semantic_graph_search` - GraphRAG with WikiLink traversal (~1-2s)
- `hybrid_search` - Parallel keyword+semantic+graph (~1-2s)

**3. Code Graph** (Semantic Code Search):
- `search_code_graph` - Find code by purpose/concept (~200-500ms)
- `query_code_structure` - Dependencies, callers, inheritance (~50-100ms)
- CLI: `.claude/scripts/code-graph-query search "pattern"`

**Decision**: Known terms → kg-search | Concepts → search_knowledge_graph | Relationships → semantic_graph_search | Code entities → search_code_graph

## What This Skill Does

### Documentation Audit
- Scans for scattered documentation files
- Identifies priority files (session summaries → test results → implementation → evaluations)
- Detects duplicate or overlapping content
- Assesses risk of knowledge loss

### Knowledge Extraction
Spawns doc-extractor agent that:
1. Reads files in priority order
2. Extracts knowledge with status tags:
   - [IMPLEMENTED] - Successfully implemented and working
   - [EXPLORED_DISCARDED] - Tried but abandoned
   - [DIDNT_WORK] - Failed approach with explanation
   - [FUTURE_IDEA] - Potential future work
3. Categorizes by target document: ARCHITECTURE.md, TESTING_GUIDE.md, PERFORMANCE_NOTES.md, etc.
4. Produces structured extraction report

### Output Report Structure

```markdown
# Knowledge Extraction Report

## For ARCHITECTURE.md
### [Component with status tags and details]

## For TESTING_GUIDE.md
### [Test coverage and outcomes]

## For PERFORMANCE_NOTES.md
### [Benchmarks and optimizations]

## Summary
- Files reviewed: X
- Knowledge items: Y
- Items by status: [IMPLEMENTED]: N, [EXPLORED_DISCARDED]: M...
```

## Integration with Workflow

### Knowledge Graph Integration
After extraction completes:
1. Review the report
2. Integrate extracted knowledge into canonical docs
3. **Create knowledge graph nodes** for reusable patterns
4. **Create code graph entries** for implementation patterns (if code-heavy)
5. Archive source files (with date prefixes: YYYY-MM-DD_name.md)
6. Sync canonical docs to project development collection

## Usage Pattern

```
/extract-docs
```

The skill will:
1. Detect documentation structure
2. Identify priority files
3. Spawn doc-extractor agent (uses existing agent template)
4. Provide extraction report path
5. Suggest next steps (consolidate → create KG nodes → archive)

## Token Efficiency

**Optimized for large documentation sets**:
- Agent runs in background (non-blocking)
- Processes files incrementally
- Produces concise report (not full file dumps)
- Expected usage: 2000-4000 tokens for 20-50 files

## Success Criteria

After extraction:
- ✅ All knowledge captured with status tags
- ✅ Clear categorization by target document
- ✅ No information loss from archival
- ✅ Reusable patterns identified for knowledge graph
