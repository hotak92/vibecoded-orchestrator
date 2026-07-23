---
name: doc-template
description: Documentation templates for README, API docs, ADRs, user guides, contributing guidelines, and changelogs. Use when asked to "create a README", "document these API endpoints", "write an architecture decision record", "draft a user guide", or "set up a changelog". Not for bespoke long-form documentation requiring manual expertise or features already documented.
short_desc: README/ADR/API-doc/user guide templates
keywords: [README template, ADR, API documentation, user guide template, contributing guidelines, "write README", "write docs", "documentation template", "architecture decision record", "changelog template"]
model: haiku
---

# Doc Template (Haiku)

Documentation templates for README, API docs, ADRs, user guides, contributing guidelines, and changelogs.

## What This Skill Does

Generates documentation from templates:
- **README.md**: Project overview, installation, usage, contribution guide
- **API Documentation**: Endpoint docs with request/response examples
- **ADRs (Architecture Decision Records)**: Decision rationale, alternatives, consequences
- **User Guides**: Step-by-step feature documentation
- **Contributing Guidelines**: Contribution process, code standards
- **Changelogs**: Version history following keep-a-changelog format

**See**: the `templates/` directory (alongside this SKILL.md) for all available templates.

## Quick Workflow Reference

**Before generating**: search for documentation template patterns.
```bash
.claude/scripts/kg-search search "template" --type concept
```

**For deep research**: run `hybrid_search("<documentation best-practices topic>")` (Weaviate MCP).

## Success Metrics

- ✅ Documentation follows best practices and is complete
- ✅ Templates are appropriate for project type
- ✅ Easy to customize with clear placeholders
