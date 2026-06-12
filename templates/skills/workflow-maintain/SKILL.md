---
name: workflow-maintain
description: Analyzes project workflow setup and suggests/creates needed automation for hooks, scripts, skills, and agents
short_desc: audit and improve project workflow automation
keywords: ["workflow automation", "project setup", "automation audit", "audit workflow", "setup workflow", "improve workflow", "add automation", "workflow improvement", "automate this task"]
tools: Read, Write, Edit, Grep, Glob, Bash, Task
model: sonnet
---

# Workflow Maintenance (Sonnet)

**Purpose**: Analyze project structure and workflow needs, then suggest or automatically create missing automation.

## When to Invoke Autonomously

1. **Starting a new project** - Bootstrap workflow from scratch
2. **Project has grown** - Needs better automation (>50 files, >3 months old)
3. **Manual repetitive tasks detected** - User doing same operations repeatedly
4. **Workflow audit requested** - Check existing automation health

## DO NOT invoke for

- Simple single-file projects
- Projects with recent workflow setup (<1 month)
- When user just wants quick implementation (organize afterward)

## Search Systems

**1. kg-search/kg-info** (Keyword/Metadata) - Fast (~100ms):
- `.claude/scripts/kg-search search "term" [--type TYPE] [--tags TAGS]`
- `.claude/scripts/kg-info info "Title"`

**2. Weaviate MCP Tools** (Semantic/Graph):
- `hybrid_search` - Keyword + semantic across KG + docs (default search tool, ~1-2s)
- `semantic_graph_search` - GraphRAG with WikiLink traversal (~1-2s)

**3. Code Graph** (Semantic Code Search):
- `search_code_graph` - Find code by purpose/concept (~200-500ms)
- `query_code_structure` - Dependencies, callers, inheritance (~50-100ms)
- CLI: `.claude/scripts/code-graph-query search "pattern"`

**Decision**: Known terms → kg-search | Concepts → hybrid_search | Relationships → semantic_graph_search | Code entities → search_code_graph

## Commands

### `/workflow-maintain analyze`
Analyze project structure and report missing workflow automation.

**Output**: List of recommended hooks, scripts, skills, and agents with rationale.

### `/workflow-maintain create [item]`
Create specific workflow automation item(s).

**Examples**:
- `/workflow-maintain create context-reminder` - Create context update reminder hook
- `/workflow-maintain create all` - Create all recommended automation
- `/workflow-maintain create hooks` - Create all recommended hooks

### `/workflow-maintain audit`
Audit existing workflow for issues.

**Checks**:
- Are hooks executable?
- Are scripts working?
- Is documentation current?
- Are there obsolete tools?

## How It Works

### 1. Detection Phase
Scans project for:
- Directory structure (docs/, tests/, knowledge/, etc.)
- File counts and types
- Existing automation
- CLAUDE.md size and complexity
- Common patterns

### 2. Analysis Phase
Determines what's missing:
- **Hooks**: Auto-triggers for common events (session-start, user-prompt-submit, post-file-edit)
- **Scripts**: Helper utilities for repeated operations (kg-search, doc-check, test-organize)
- **Skills**: User-invocable expertise
- **Agents**: Long-running workflows

### 3. Creation Phase
Generates customized automation:
- Uses templates from global workflow
- Customizes with project-specific paths/config
- Makes files executable
- Updates documentation

### 4. Testing Phase
Validates generated automation:
- Runs syntax checks
- Tests execution
- Verifies integration

## Detection Rules

### Context Reminder Hook
**Trigger**: Project has CONTEXT_STATE.md
**Creates**: user-prompt-submit.sh hook
**Purpose**: Remind to update context during active work

### Session Start Hook
**Trigger**: Project has knowledge graph OR complex setup
**Creates**: session-start.sh hook
**Purpose**: Auto-load relevant context at session start

### Post-Edit Hook
**Trigger**: Project has knowledge/, tests/, or docs/
**Creates**: post-file-edit.sh hook
**Purpose**: Auto-sync, run tests, update docs

### Doc Check Script
**Trigger**: docs/ has >10 files OR CLAUDE.md >800 lines
**Creates**: doc-check script
**Purpose**: Check documentation is in sync with code

### Knowledge Graph Scripts
**Trigger**: knowledge/ directory exists
**Creates**: kg-search, kg-info, kg-sync scripts
**Purpose**: Interact with knowledge graph efficiently

### Code Graph Scripts
**Trigger**: Large codebase (>30 source files) OR polyglot project
**Creates**: code-graph-analyze, code-graph-query scripts
**Purpose**: Semantic code search and structural queries

### Smart File Operations
**Trigger**: Large codebase (>50 source files)
**Creates**: smart_file_ops.py script
**Purpose**: Token-efficient file operations

## Workflow Integration

### Knowledge Graph Support
Projects with knowledge/ directory get:
- kg-search (keyword/metadata search)
- kg-info (node details and connections)
- kg-sync (sync to Weaviate)
- session-start hook (auto-load relevant KG context)

### Code Graph Support
Projects with large codebases get:
- code-graph-analyze (AST-based entity extraction)
- code-graph-query (semantic and structural search)
- Search by purpose/concept, not just text
- Dependency analysis, call graphs, inheritance

### Weaviate Collections
Projects with docs/ directory get:
- Auto-sync to [Project]_development collection
- post-file-edit hook for automatic updates
- Search tools via Weaviate MCP

## Examples

### Example 1: Bootstrap New Project
```
User: /workflow-maintain analyze
Claude: [Scans project, shows recommendations]

User: /workflow-maintain create all
Claude: [Creates all recommended automation]
  - Created: .claude/hooks/user-prompt-submit.sh
  - Created: .claude/hooks/session-start.sh
  - Created: .claude/scripts/doc-check
  - Updated: .claude/CLAUDE.md
  - Updated: .claude/settings.json
```

### Example 2: Add Specific Automation
```
User: /workflow-maintain create context-reminder
Claude: [Creates context reminder hook]
  - Created: .claude/hooks/user-prompt-submit.sh
  - Configured: Every 10 messages, 15min threshold
  - Updated: .claude/settings.json
```

### Example 3: Audit Existing
```
User: /workflow-maintain audit
Claude: [Audits project workflow]
  ✅ Hooks are executable
  ✅ Scripts working correctly
  ⚠️  CLAUDE.md is 950 lines (recommend splitting)
  ⚠️  test-organize script not used in 45 days
  ❌ doc-check script has syntax error

  Recommendations:
  - Extract sections from CLAUDE.md to docs/
  - Fix doc-check script
  - Consider removing unused test-organize
```

## Customization

Each generated file includes project-specific:
- **Paths**: Absolute paths to project directories
- **Config**: Project-specific settings
- **Constraints**: Resource limits, file patterns
- **Documentation**: How to use in this project

## Integration with helper-scripter

When creating new types of automation:
1. Use `/workflow-maintain` for standard patterns
2. Spawn helper-scripter agent for custom automation
3. helper-scripter can create templates for future use

**Division of labor**:
- **workflow-maintain**: Standard, proven patterns
- **helper-scripter**: Novel automation for specific needs

## Best Practices

### DO
- Run analyze before creating anything
- Review recommendations before accepting
- Test generated automation immediately
- Audit periodically (monthly)

### DON'T
- Create automation you won't use
- Override working custom tools
- Skip testing phase
- Forget to update documentation
- Create project-specific versions of global tools

## Token Efficiency

**Optimized for low token usage**:
- Uses detection script (analyze without reading files)
- Parallel file checks when needed
- Generates files without showing full contents
- Concise status updates
- Delegates complex generation to scripts

**Expected usage**: 1000-2000 tokens per invocation

## Success Metrics

After using workflow-maintain, project should have:
- All repetitive tasks automated
- Context properly maintained
- Tests organized efficiently
- Documentation current
- Token usage optimized

**Measure improvement**:
- Time saved per session
- Tokens saved per session
- Fewer manual operations
- Less context confusion
- Better organization
