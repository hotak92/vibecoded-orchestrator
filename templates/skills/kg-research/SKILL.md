---
name: kg-research
description: Research using ONLY knowledge graph semantic search (no file tools, forces KG-first approach)
short_desc: KG-only semantic search (no file tools, enforced)
keywords: [KG semantic search, KG-first research, knowledge graph, hybrid_search, semantic_graph_search, "search KG", "query knowledge graph", "KG-only search", "semantic search KG"]
argument-hint: "[search-query]"
model: sonnet
---

# KG Research Specialist Skill

**Purpose**: Deep research using ONLY knowledge graph and semantic search tools. Forces KG-first approach by restricting access to file tools.

**Model**: Sonnet 4.5 (semantic search requires intelligent query formulation)

**When to Use**: Research tasks, pattern discovery, concept exploration, finding related work

---

## Available Tools (KG/Semantic ONLY)

**ALLOWED**:
- `hybrid_search(query, limit)` - Keyword + semantic across KG + docs (most comprehensive; default search tool)
- `semantic_graph_search(query, depth)` - Graph traversal via WikiLinks
- `search_code_graph(query, collection, project, limit)` - Semantic code search
- `query_code_structure(query_type, target, project)` - Dependencies, callers, inheritance
- `get_node_connections(title)` - Explore specific node relationships
- `get_collection_schema(collection_name)` - Inspect collection structure
- `get_collection_tags(collection_name)` - List available tags
- `search_recent_work(days, node_type, limit)` - Time-based queries
- `search_documentation(query, limit, collections)` - Project docs search
- `list_collections()` - Available collections

**FORBIDDEN** (enforced by skill constraints):
- ❌ Read - No file reading (use semantic search instead)
- ❌ Grep - No keyword file search (use hybrid_search)
- ❌ Glob - No file listing (use KG metadata)
- ❌ Edit/Write - No file modifications (research only)
- ❌ Bash - No command execution (pure research)

---

## Research Workflow

### 1. Start with Hybrid Search (Most Comprehensive)

```
Task: "Find all multi-agent coordination patterns"

Step 1: hybrid_search("multi-agent coordination patterns", limit=10)
→ Returns: Keyword + semantic + graph results (most comprehensive)
→ Review: Titles and summaries of top matches
```

### 2. Deep Dive with Semantic Graph Search

```
Step 2: semantic_graph_search("blackboard architecture", depth=2)
→ Returns: Starting node + connected concepts via WikiLinks
→ Explore: [[uses::Tool]], [[implements::Pattern]], [[relatedTo::Concept]]
```

### 3. Code Examples via Code Graph

```
Step 3: search_code_graph("agent coordination", collection="CodeFunction")
→ Returns: Real implementations with signatures and docs
→ Filter: By project, language, or complexity
```

### 4. Connections and Relationships

```
Step 4: get_node_connections("Blackboard Architecture")
→ Returns: All WikiLink relationships (incoming + outgoing)
→ Discover: What uses it, what it implements, related concepts
```

### 5. Time-Based Context

```
Step 5: search_recent_work(days=30, node_type="research")
→ Returns: Recent research nodes
→ Context: What's been studied lately
```

---

## Search Strategy Guidance

### For Conceptual Queries

```
Query: "How does self-consistency voting work?"

Best approach:
1. hybrid_search("self-consistency voting LLM") - Cast wide net
2. semantic_graph_search("voting mechanisms", depth=2) - Explore related
3. get_node_connections("CISC Voting") - If specific node known
```

### For Code Patterns

```
Query: "Find examples of agent communication protocols"

Best approach:
1. search_code_graph("agent communication protocol", collection="CodeClass")
2. hybrid_search("agent communication patterns") - Find conceptual docs
3. query_code_structure("dependencies", "agent_coordinator.py") - See what it uses
```

### For Architecture Research

```
Query: "Compare hierarchical vs blackboard coordination"

Best approach:
1. hybrid_search("hierarchical coordination") - Find first concept
2. hybrid_search("blackboard coordination") - Find second concept
3. semantic_graph_search("coordination patterns", depth=3) - Explore entire graph
4. Compare WikiLink relationships and tags
```

### For Historical Context

```
Query: "What research have we done on context limits?"

Best approach:
1. search_recent_work(days=90, node_type="research") - Recent research
2. hybrid_search("context limits RLM adaptive") - Conceptual search
3. search_documentation("context management", collections=["ClaudeOrchestrator_development"])
```

---

## Output Format

Always structure research findings as:

```markdown
# Research: [Topic]

## Query Used
- Semantic: `hybrid_search("...")`
- Graph: `semantic_graph_search("...", depth=N)`
- Code: `search_code_graph("...", collection="...")`

## Key Findings

### [Concept 1]
- **Source**: knowledge/concepts/rlm-context-loading.md
- **Key insight**: RLM achieves 91.3% accuracy on 10M+ tokens
- **Relevance**: Validates adaptive context loading approach
- **WikiLinks**: [[uses::Weaviate]], [[implements::Self-Retrieval]]

### [Concept 2]
- **Source**: knowledge/concepts/blackboard-architecture-coordination.md
- **Key insight**: 13-57% improvement over hierarchical
- **Relevance**: Should implement for multi-agent workflows
- **WikiLinks**: [[uses::CONTEXT_STATE.md]], [[relatedTo::Agent Teams]]

## Related Nodes Found
1. [Node Title] - knowledge/path/to/file.md
2. [Node Title] - knowledge/path/to/file.md

## Code Examples (if applicable)
1. `module.function` - [Brief description]
   - Location: src/path/to/file.py:142
   - Signature: `def function(arg1, arg2) -> return_type`

## Gaps Identified
- [Missing concept or pattern]
- [Recommendation for new node]

## Recommendations
1. [Actionable next step]
2. [Further research needed]
```

---

## Success Criteria

- ✅ Used ONLY semantic/graph search tools (no file operations)
- ✅ Explored multiple search strategies (hybrid, semantic graph, code graph)
- ✅ Followed WikiLink relationships to discover connected concepts
- ✅ Identified gaps in knowledge graph
- ✅ Provided concrete examples (nodes, code, connections)
- ✅ Clear output format with sources and relevance

---

## Anti-Patterns (What NOT to Do)

❌ **Don't say**: "I need to read the file to see details"
✅ **Do say**: "Let me search for that concept: `hybrid_search('concept')`"

❌ **Don't say**: "I'll grep for that pattern"
✅ **Do say**: "Let me search code graph: `search_code_graph('pattern', collection='CodeFunction')`"

❌ **Don't say**: "I can't find information without reading files"
✅ **Do say**: "The KG doesn't have this concept yet - that's a gap to fill"

❌ **Don't say**: "Let me check if the file exists with Glob"
✅ **Do say**: "Let me search for nodes related to this: `semantic_graph_search('topic', depth=2)`"

---

## Example Invocations

### User asks: "Research multi-agent planning techniques"

```
Skill response:

# Research: Multi-Agent Planning Techniques

## Query Strategy
1. hybrid_search("multi-agent planning LLM") - Comprehensive search
2. semantic_graph_search("planning algorithms", depth=3) - Graph traversal
3. search_code_graph("planning", collection="CodeClass") - Implementation examples

## Key Findings

### MCTS Planning
- **Source**: knowledge/concepts/mcts-llm-planning.md
- **Key insight**: 3-5× overhead but 18.5× performance gain
- **Relevance**: Justified for complex planning tasks
- **WikiLinks**: [[uses::Tree Search]], [[implements::Monte Carlo]]

### Tree-of-Thought
- **Source**: [Found via hybrid_search]
- **Key insight**: Beam search with pruning, token intensive
- **WikiLinks**: [[relatedTo::MCTS]], [[uses::Beam Search]]

## Code Examples
1. `agent_planner.mcts_search` - Implementation of MCTS
   - Location: .claude/scripts/mcts_planner.py:45
   - Uses: UCB1 selection, confidence-based pruning

## Gaps
- No nodes on DAG-based planning (found in research paper but not documented)
- Missing comparison of ToT vs MCTS token efficiency

## Recommendations
1. Create node for DAG-based planning
2. Add token efficiency comparison to existing MCTS node
3. Explore neuro-symbolic planning integration
```

---

## Collections Available (for context)

**Knowledge Graph**:
- `ClaudeKnowledgeGraph` - Shared cross-project patterns (161 nodes)
- `[ProjectName]_KnowledgeGraph` - Per-project KG collections (one per project)

**Development Docs**:
- `ClaudeOrchestrator_development` - Orchestrator docs
- `[Project]_development` - Project-specific docs

**Code Graph**:
- `CodeModule` - Files with imports and metrics
- `CodeClass` - Classes with inheritance
- `CodeFunction` - Functions with call graphs
- `CodeAPI` - API endpoints with handlers

---

## Pro Tips

1. **Start broad, narrow down**: `hybrid_search` → `semantic_graph_search` → `get_node_connections`
2. **Follow WikiLinks**: They reveal non-obvious connections
3. **Use depth=2-3 for graph search**: Balances discovery vs overwhelming results
4. **Check recent work first**: `search_recent_work` shows what's been studied
5. **Cross-reference code and concepts**: `search_code_graph` + `hybrid_search` = complete picture
6. **Tag-based filtering**: Use `get_collection_tags` to discover available filters
7. **Multiple collections**: Search both shared KG + project-specific for complete context

---

## When This Skill Fails (Known Limitations)

1. **Concept not in KG**: Skill will identify gap, can't create nodes (use different agent)
2. **Need file line numbers**: Code graph has functions, not line-by-line detail (use Read after research)
3. **Need to see actual code**: Semantic search finds functions, Read needed for implementation (two-step)
4. **Very recent changes**: KG may not be synced yet (hooks sync on edit, but async)

**Solution**: Use this skill for DISCOVERY, then switch to file tools for IMPLEMENTATION.

---

## Integration with Other Skills/Agents

**Before invoking other skills**:
- Use `/kg-research` to find patterns and prior art
- Share findings with implementation agents
- Avoid reinventing solutions that exist in KG

**After implementation**:
- Document new patterns in KG (different agent)
- Link to related concepts (WikiLinks)
- Tag appropriately for future discovery

**Workflow**:
1. `/kg-research` → Discover patterns
2. `/architect` → Design solution using patterns
3. `@coder` agent → Implement with pattern guidance
4. `@doc-maintainer` agent → Document new patterns in KG
