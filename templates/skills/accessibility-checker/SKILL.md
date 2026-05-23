---
name: accessibility-checker
description: Quick A11y review - WCAG 2.1 checklist, screen reader compatibility, keyboard navigation, color contrast
short_desc: WCAG-2.1 / a11y compliance checklist review
keywords: [WCAG, A11y, accessibility, screen reader, ARIA, color contrast, usability]
model: haiku
---

# Accessibility Checker (Haiku)

**Purpose**: Quick A11y review - WCAG 2.1 checklist, screen reader compatibility, keyboard navigation, color contrast.

**Model**: Haiku 4.5 (fast accessibility checking)

**When to Invoke Autonomously**:

1. **Quick A11y Review**: "Check accessibility of [component/page]"
2. **WCAG Compliance**: "Does this meet WCAG 2.1 AA?"
3. **Screen Reader Test**: "Will screen readers handle this correctly?"
4. **Keyboard Navigation**: "Can users navigate without mouse?"

**DO NOT invoke for**:
- Comprehensive audits (use specialized tools)
- Already-accessible code

## Usage

```
/accessibility-checker review [component]
/accessibility-checker wcag-check [page/component]
/accessibility-checker keyboard-nav [interactive elements]
/accessibility-checker contrast-check [colors]
```

## What This Skill Does

Reviews code for:
- **Semantic HTML**: Proper element usage (`<button>` not `<div>`)
- **ARIA Labels**: Descriptive labels for screen readers
- **Keyboard Navigation**: Tab order, focus management, keyboard shortcuts
- **Color Contrast**: WCAG 2.1 ratios (AA: 4.5:1 normal, 3:1 large text)
- **Alt Text**: Descriptive image alternatives
- **Form Labels**: Explicit label-input associations

For code examples, see [examples/](examples/).

## Output Format

See [template.md](template.md) for structured review format.

## Supporting Files

- **Examples**: See [examples/good-examples.md](examples/good-examples.md) and [examples/bad-examples.md](examples/bad-examples.md)
- **Template**: Use [template.md](template.md) for structured reviews
- **Contrast Checker**: Run `python scripts/check-contrast.py <color1> <color2>` to check WCAG compliance

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "accessibility" --type concepts
```

**For deep research**: Ask user "Use hybrid_search to research [a11y topic]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Success Metrics

- ✅ All WCAG 2.1 AA issues identified
- ✅ Recommendations are actionable
