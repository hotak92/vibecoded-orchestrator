---
name: doc-template
description: Documentation templates - README, API docs, ADRs, user guides
keywords: [README template, ADR, API documentation, user guide template, contributing guidelines]
model: haiku
---

# Doc Template (Haiku)

**Purpose**: Documentation templates - README, API docs, ADRs, user guides.

**Model**: Haiku 4.5 (fast template generation)

**When to Invoke Autonomously**:

1. **README Generation**: "Create README for [project]"
2. **API Documentation**: "Document API endpoints"
3. **Architecture Decision Records**: "Document decision for [choice]"
4. **User Guides**: "Create user guide for [feature]"

**DO NOT invoke for**:
- Complex documentation projects (manual writing)
- Already-documented features

## DO NOT invoke for

- Complex documentation projects requiring manual expertise
- Already-documented features
- One-off documentation (use templates directory directly)

## What This Skill Does

Generates documentation from templates:
- **README.md**: Project overview, installation, usage, contribution guide
- **API Documentation**: Endpoint docs with request/response examples
- **ADRs (Architecture Decision Records)**: Decision rationale, alternatives, consequences
- **User Guides**: Step-by-step feature documentation
- **Contributing Guidelines**: Contribution process, code standards
- **Changelogs**: Version history following keep-a-changelog format

**See**: `templates/` directory for all available templates

## Quick Workflow Reference

**Before generating**: Search for documentation template patterns
```bash
.claude/scripts/kg-search search "template" --type concepts
```

**For deep research**: Ask user "Use hybrid_search to research [documentation best practices]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Success Metrics

- ✅ Documentation follows best practices and is complete
- ✅ Templates are appropriate for project type
- ✅ Easy to customize with clear placeholders
