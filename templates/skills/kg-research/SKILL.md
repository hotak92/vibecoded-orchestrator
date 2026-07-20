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

**Model**: Sonnet (semantic search requires intelligent query formulation)

**When to Use**: Research tasks, pattern discovery, concept exploration, finding related work

---

## Available Tools (weaviate-kg MCP — the real tool surface)

**ALLOWED** (read/search):
- `hybrid_search(query, limit=5, node_type=None, tags=None, days=None, detail="auto")` — combined keyword + semantic search across the per-project KG, shared KG, and project docs (collections are scoped automatically — no collection parameter needed). **Default and first search tool.** `node_type` filters by type (`project`, `concept`, `tool`, `model`, `hardware`, `research`); `tags` filters by tag list; `days=N` restricts to recently-updated nodes.
- `semantic_graph_search(query, limit=5, depth=2, detail="auto")` — GraphRAG: semantic matches PLUS their connected neighbors via typed WikiLinks (`uses`, `implements`, `extends`, `buildsOn`, `relatedTo`). Use for "what relates to X?", dependency chains, and exploring a node's connections. `depth` max 3.
- `search_code_graph(query, scope="all", limit=8, expand_hops=0, layer=None, project=None, detail="auto")` — find code by purpose/concept. `scope` is `"all"` | `"code"` (functions/classes/modules) | `"interaction"` (APIs, cross-service calls). `expand_hops` 1-2 follows call edges from the seed results.
- `query_code_structure(query_type, target, project=None)` — exact structural queries: `dependencies`, `imports`, `callers`, `methods`, `extends`, `interactions`, `path` (target `"source.func->dest.func"`), `composes`, `composed_by`, `type_users`.

**Available but out of scope for research** (this skill is read-only):
- `store_knowledge_node(title, content, node_type, tags, links, file_path, scope)` — the KG write tool. Use it AFTER research, or hand findings to a documentation agent, to persist newly-identified gaps as nodes.
- `describe_excalidraw(file_path)` — describes an Excalidraw diagram file; only relevant when a node references one.

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
→ Returns: keyword + semantic results across project KG, shared KG, and docs
→ Review: titles, scores, and per-result detail tier
```

### 2. Deep Dive with Semantic Graph Search

```
Step 2: semantic_graph_search("blackboard architecture", depth=2)
→ Returns: primary matches + connected concepts via WikiLinks
→ Explore: [[uses::Tool]], [[implements::Pattern]], [[relatedTo::Concept]]
```

### 3. Code Examples via Code Graph

```
Step 3: search_code_graph("agent coordination", scope="code")
→ Returns: real implementations with signatures and summaries
→ Narrow: expand_hops=1 to pull in direct callers/callees
```

### 4. Connections and Relationships

```
Step 4: semantic_graph_search("Blackboard Architecture", depth=2)
→ Returns: the node plus its WikiLink neighborhood (incoming + outgoing)
→ Discover: what uses it, what it implements, related concepts
```

### 5. Time-Based Context

```
Step 5: hybrid_search("recent research", node_type="research", days=30)
→ Returns: research nodes updated in the last 30 days
→ Context: what's been studied lately
```

---

## Search Strategy Guidance

### For Conceptual Queries

```
Query: "How does self-consistency voting work?"

Best approach:
1. hybrid_search("self-consistency voting LLM") - Cast wide net
2. semantic_graph_search("voting mechanisms", depth=2) - Explore related
3. semantic_graph_search("CISC Voting", depth=1) - If specific node known
```

### For Code Patterns

```
Query: "Find examples of agent communication protocols"

Best approach:
1. search_code_graph("agent communication protocol", scope="code")
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
1. hybrid_search("context limits", node_type="research", days=90) - Recent research
2. hybrid_search("context limits adaptive loading") - Conceptual search
   (project docs are included automatically — no separate docs tool)
```

---

## Output Format

Always structure research findings as:

```markdown
# Research: [Topic]

## Query Used
- Semantic: `hybrid_search("...")`
- Graph: `semantic_graph_search("...", depth=N)`
- Code: `search_code_graph("...", scope="...")`

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
✅ **Do say**: "Let me search code graph: `search_code_graph('pattern', scope='code')`"

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
3. search_code_graph("planning", scope="code") - Implementation examples

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

## Collections Searched (for context)

`hybrid_search` scopes these automatically — you never pass a collection name:

**Knowledge Graph**:
- `<ProjectName>_KnowledgeGraph` — the per-project KG (name from the `KG_COLLECTION` env var)
- Shared cross-project KG (name from `SHARED_KG_COLLECTION`, default `VibeCodedOrchestrator_KnowledgeGraph`) — auto-merged into every read

**Development Docs**:
- `<ProjectName>_development` — verbose project docs (name from `DEVELOPMENT_COLLECTION`)

**Code Graph** (searched by `search_code_graph` / `query_code_structure`):
- `CodeModule` - Files with imports and metrics
- `CodeClass` - Classes with inheritance
- `CodeFunction` - Functions with call graphs
- `CodeAPI` - API endpoints with handlers
- `CodeInteraction` - Cross-service calls (HTTP, gRPC, message queues)

---

## Pro Tips

1. **Start broad, narrow down**: `hybrid_search` → `semantic_graph_search` → targeted `semantic_graph_search(depth=1)` on a known node title
2. **Follow WikiLinks**: They reveal non-obvious connections
3. **Use depth=2-3 for graph search**: Balances discovery vs overwhelming results
4. **Check recent work first**: `hybrid_search(..., days=30)` shows what's been studied lately
5. **Cross-reference code and concepts**: `search_code_graph` + `hybrid_search` = complete picture
6. **Filter by type and tags**: `node_type` and `tags` on `hybrid_search` narrow noisy result sets
7. **Trust the auto-scoping**: per-project and shared collections are merged for you — no need to search each separately

---

## When This Skill Fails (Known Limitations)

1. **Concept not in KG**: Skill will identify the gap; persist it afterwards via `store_knowledge_node` or hand off to a documentation agent
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
- Document new patterns in KG (`store_knowledge_node`, or a documentation agent)
- Link to related concepts (WikiLinks)
- Tag appropriately for future discovery

**Workflow**:
1. `/kg-research` → Discover patterns
2. `/architect` → Design solution using patterns
3. `@coder` agent → Implement with pattern guidance
4. `@doc-maintainer` agent → Document new patterns in KG
