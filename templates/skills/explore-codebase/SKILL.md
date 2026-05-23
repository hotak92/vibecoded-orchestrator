---
name: explore-codebase
description: Systematic codebase onboarding. Builds a mental model of a new or unfamiliar project by exploring structure, architecture, key data models, entry points, and auth patterns.
keywords: ["unfamiliar codebase", "codebase onboarding", "architectural overview", "entry point", "new project", "codebase audit"]
argument-hint: "[project-path-or-question]"
model: sonnet
---

# /explore-codebase [path or project name]

Structured onboarding for unfamiliar codebases. Produces a concise architecture summary.

## Usage

```
/explore-codebase                           # explore current directory
/explore-codebase /path/to/project
/explore-codebase MultiagentOrchestrator    # named project (searches known paths)
```

## Exploration Sequence

### 1. Project Overview (~2 min)

The commands below assume a Unix shell (Linux/macOS, or Git Bash on Windows).
On native Windows shells, substitute `dir` for `ls -la` and `type` /
`Get-Content` for `cat`; `git`, `head`, and the rest behave the same.

```bash
ls -la                              # root structure (Windows: `dir` or PowerShell `Get-ChildItem`)
cat README.md | head -80            # stated purpose (Windows: `Get-Content README.md -TotalCount 80`)
cat pyproject.toml | head -40       # or package.json / Cargo.toml
git log --oneline -10               # recent activity (cross-platform via git)
git shortlog -sn --no-merges | head -5  # main contributors
```

### 2. Directory Structure

Map the top-level directories:
- Where is the entry point? (main.py, app.py, index.ts, src/)
- Where are tests?
- Where is configuration?
- Any generated code or build artifacts?

### 3. Architecture Patterns

**Check KG first** (fastest if project is known):
```
search_knowledge_graph("project architecture [name]")
search_code_graph("main entry point", collection="CodeModule")
```

**Then read**:
- Entry point file (first 50 lines)
- Main data models / schemas
- Core module `__init__.py` or barrel files

### 4. Key Data Flows

Trace the primary request/data path:
- Input → processing → output
- Where does data enter? Where does it exit?
- Any queues, background jobs, or async paths?

### 5. Configuration & Env

```bash
cat .env.example 2>/dev/null || ls *.env* *.yaml *.toml 2>/dev/null
```
What must be configured to run locally?

### 6. Auth & Security (if applicable)

- How are users authenticated?
- Where are secrets stored/accessed?
- Any MCP servers or external API calls?

### 7. Test Strategy

```bash
ls tests/ 2>/dev/null && cat tests/conftest.py 2>/dev/null | head -30
pytest tests/ --collect-only 2>/dev/null | head -20
```
- Test coverage? Test patterns (unit/integration/e2e)?
- How to run tests?

## Output Format

```markdown
# Codebase Overview: [Project Name]

**Purpose**: [one sentence]
**Stack**: [language, frameworks, databases]
**Entry point**: `[file:line]`

## Architecture

[Diagram or bullet description of major components and their relationships]

## Key Data Models
- `[ModelName]` — [what it represents, where defined]

## Data Flow
[input] → [processing steps] → [output]

## Configuration Required
- `ENV_VAR` — [what it controls]

## How to Run
```bash
[commands to install deps and start]
```

## Test Command
```bash
[test command]
```

## Things to Know
- [gotchas, non-obvious design decisions, known issues]
```

Save to `CODEBASE_OVERVIEW.md` or print to chat.
