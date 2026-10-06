---
name: accessibility-checker
description: Quick accessibility review of UI code against a WCAG 2.1 AA checklist - semantic HTML, ARIA labels, keyboard navigation, focus management, color contrast, alt text, and form labels. Use when reviewing a component or page for a11y, checking WCAG compliance, or verifying screen-reader and keyboard support. For a full audit with automated tooling, hand off to a dedicated accessibility suite instead.
short_desc: WCAG-2.1 / a11y compliance checklist review
keywords: [WCAG, A11y, "screen reader", ARIA, "color contrast", "check accessibility", "WCAG compliance"]
model: haiku
---

# Accessibility Checker (Haiku)

**Purpose**: Quick A11y review - WCAG 2.1 checklist, screen reader compatibility, keyboard navigation, color contrast.

**Model**: Haiku (fast accessibility checking)

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
.claude/scripts/kg-search search "accessibility" --type concept
```

**For deep research**: `hybrid_search("[a11y topic]")` (Weaviate MCP)

## Success Metrics

- ✅ All WCAG 2.1 AA issues identified
- ✅ Recommendations are actionable
