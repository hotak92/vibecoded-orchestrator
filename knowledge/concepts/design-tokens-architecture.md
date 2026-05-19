---
title: Design Tokens Architecture
type: concept
tags:
- design
- design-tokens
- design-system
- frontend
- mid-level-architecture
- DTCG
- W3C
- brand
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Design Tokens Architecture

Design tokens are named, machine-readable design decisions — color values, spacing, type sizes, motion durations — stored once and consumed across Figma, web, iOS, Android, print specs. A token system replaces "the brand red is #C8102E" copy-pasted in 40 places with a single source that propagates everywhere.

## Why tokens matter

Without tokens:
- Designers and engineers re-derive the same values from screenshots
- Brand updates require manual sweeps across N codebases and M Figma files
- Dark mode, RTL, and accessibility variants multiply the surface to maintain
- Drift is inevitable; consistency is hand-policed and lossy

With tokens:
- One source of truth, transformed automatically to every platform's native format
- Brand change = edit one JSON, ship everywhere
- Variant systems (light/dark, density, brand-A/brand-B) compose cleanly
- Drift becomes a lint error, not a vibe

## DTCG — the W3C interchange format

The W3C Design Tokens Community Group ([DTCG](https://www.w3.org/community/design-tokens/)) publishes the emerging interchange standard. Specification still in active work as of 2026, but the format is stable enough for production use and is supported by Style Dictionary, Tokens Studio, Specify, and most modern design system tools.

```json
{
  "color": {
    "brand": {
      "primary": {
        "$value": "#0066cc",
        "$type": "color",
        "$description": "Primary brand color"
      }
    }
  }
}
```

Required: `$value`, `$type`. Optional: `$description`, `$extensions` (vendor-specific metadata).

Token types defined in DTCG: `color`, `dimension`, `fontFamily`, `fontWeight`, `duration`, `cubicBezier`, `number`, plus composite types `strokeStyle`, `border`, `transition`, `shadow`, `gradient`, `typography`.

## Three-tier architecture

Mature systems separate token layers by intent. Each tier references the one above it.

### Tier 1: Primitive (palette)
Every value the brand owns, no semantic meaning. Just a catalogue.

```
color.neutral.100 = #f5f5f5
color.neutral.500 = #737373
color.neutral.900 = #171717
color.blue.500 = #0066cc
color.red.500 = #dc2626
```

**Rule**: UI code should rarely reference primitives directly. Primitives are an internal palette; semantic tokens are the public API.

### Tier 2: Semantic (intent)
Names encode purpose. Values reference primitives.

```
color.text.primary = {color.neutral.900}
color.text.secondary = {color.neutral.600}
color.surface.default = {color.neutral.50}
color.surface.raised = #ffffff
color.action.primary.bg = {color.blue.500}
color.action.primary.text = #ffffff
color.feedback.error = {color.red.500}
```

**Rule**: 80% of UI code consumes tier-2 tokens. This is where dark mode lives — same semantic name, different primitive references.

### Tier 3: Component (optional)
References semantics, useful for big systems where the same semantic is consumed by many components.

```
button.primary.bg = {color.action.primary.bg}
button.primary.text = {color.action.primary.text}
button.primary.bg-hover = {color.blue.600}
```

**Rule**: skip tier 3 unless the system has >50 components or multiple brands. Premature tier-3 = bureaucracy.

## Reference patterns

[[implements::W3C DTCG Specification]]
[[uses::Style Dictionary]]
[[uses::Tokens Studio for Figma]]
[[relatedTo::Material 3 Design Tokens]]
[[relatedTo::Color Management for Designers]]

Material 3 documents a token system at https://m3.material.io/foundations/design-tokens/overview — referenced as a working example of the three-tier pattern at scale.

## Tooling

- **Style Dictionary** (Amazon, open source) — transforms DTCG JSON to CSS variables, Sass, iOS Swift, Android XML, Flutter Dart, plain JSON, anything. Pipeline tool.
- **Tokens Studio for Figma** — edit DTCG-compatible tokens inside Figma, syncs to Git.
- **Specify** — commercial design-token platform, multi-source ingestion, multi-format export.
- **Supernova** — design system platform with token export.

## Variant systems (the payoff)

A semantic-token layer lets the same UI code render multiple variants by swapping the token source.

```
# Light mode
color.text.primary = {color.neutral.900}
color.surface.default = #ffffff

# Dark mode
color.text.primary = {color.neutral.50}
color.surface.default = {color.neutral.950}
```

Same `color.text.primary` reference; different file. No UI code change.

Variants compose: light/dark × default/high-contrast × density-comfortable/density-compact = 8 variants from one component implementation, if the token layer is well-factored.

## Contrast pair validation

Token tooling enables systematic contrast checking. Every pair (text-token, bg-token) actually used in components can be validated against WCAG 2.2 AA (4.5:1 body, 3:1 large/UI) at build time. APCA (the perceptual contrast algorithm being studied for WCAG 3) is supported by some tools — it's not yet normative but it correlates better with perceived readability than the legacy luminance ratio.

## Anti-patterns

- **One-tier token sprawl** — flat list of `color-red-1`, `color-red-2`, ..., `color-red-47`. No intent encoded. Use two-tier.
- **Naming after appearance, not intent** — `color.gray-light` instead of `color.text.disabled`. Locks the system; intent names survive a palette refresh.
- **Same value, two names** — `color.brand.primary` and `color.button.primary` both = `#0066cc`, with no alias relation. Causes drift when one updates and not the other. Use references, not duplicates.
- **Hand-maintaining platform outputs** — writing the CSS variables AND the Swift file AND the Android XML. Use Style Dictionary or equivalent to derive all from one DTCG file.
- **Tokens defined but not adopted** — code still hardcodes. Run a codemod; lint future hardcoded values.

## When NOT to formalize

For projects under ~10 components or one-off marketing sites, formal token architecture is overhead. CSS custom properties + a `theme.css` file is enough. Introduce DTCG when:
- You have 2+ surfaces (web + iOS, or marketing + app)
- You ship dark mode or other variants
- You have >1 designer or >2 engineers touching styling
- Brand updates happen ≥yearly

## See also

- Brand identity systems → `knowledge/concepts/color-management-for-designers.md` for cross-medium color authoring
- WCAG 2.2 contrast → https://www.w3.org/TR/WCAG22/
- DTCG draft spec → https://www.w3.org/community/design-tokens/
