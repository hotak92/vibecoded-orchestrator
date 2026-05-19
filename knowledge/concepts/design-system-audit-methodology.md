---
title: Design System Audit Methodology
type: concept
tags:
- design
- design-system
- design-tokens
- audit
- mid-level-architecture
- WCAG
- Figma
- DTCG
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Design System Audit Methodology

A design system audit is a structured inventory of where a system's stated rules diverge from its lived reality. The audit produces a prioritized fix list with effort estimates, not a redesign. Use this methodology when inheriting a system, before a major redesign, when designers and developers report "the colors don't match" or "spacing feels off," or as a pre-launch accessibility check of the system itself (not individual pages).

## The eight audit axes

In priority order — fix problems in the top axes before moving down.

1. **Token source-of-truth** — Is there one? Is it DTCG JSON, Figma variables, a Tailwind config, a brand-book PDF? Which one wins when they disagree? Without a single canonical source, every other audit finding is symptomatic.
2. **Color tokens** — Count, naming convention, primitive-vs-semantic split, contrast-pair validity (WCAG 2.2 AA minimum, 7:1 for AAA targets), cross-space coverage (sRGB / Display P3 / dark mode).
3. **Type tokens** — Scale ratio, line-height per size, weight set, fallback stacks, license coverage (web / print / embed).
4. **Spacing tokens** — Single scale or multiple? Snap to a baseline grid? Documented or assumed? Off-scale values like `padding: 13px` are the smoking gun.
5. **Component inventory** — How many buttons exist *in the codebase* (not just in Figma)? Are they tokenized or hardcoded? Near-duplicates from "the original wasn't flexible enough."
6. **Figma ↔ code parity** — Same names, same values, same intent. Drift is normal; **unacknowledged drift** is the problem.
7. **Accessibility of the system itself** — Focus indicators tokenized? Reduced-motion honored? RTL-safe? Color-blind safe (status colors verified for Deuteranopia / Protanopia / Tritanopia)?
8. **Documentation** — Where do designers find the rules? Are do/don't pairs current? Static brand-book PDFs that nobody updates are anti-patterns.

## Audit process

### Step 1: Locate the sources
Find every place tokens and styles live:

- Figma — Variables, Local Styles, Library
- Codebase — search for `tokens.json`, `theme.ts`, `tailwind.config.*`, `:root { --` in CSS, design-system packages, Storybook
- Brand book PDF (often the "official" reference but rarely up to date)

Useful searches:

```
Glob "**/tokens*.json"
Glob "**/theme.{ts,js,json}"
Glob "**/tailwind.config.*"
Grep -r "rgb\(|#[0-9a-fA-F]{3,8}|hsl\(" --include="*.{css,scss,ts,tsx,jsx}"
```

Output a **sources matrix** — what lives where, what depth (token / mixin / hardcoded), last-modified date.

### Step 2: Count and compare
For each token type:

**Colors** — extract from Figma export AND from codebase. Compare:
- Tokens in Figma not in code
- Tokens in code not in Figma
- Same name, different value (drift)
- Same value, different name (alias proliferation)

Heuristic: a healthy mid-sized system has 30–80 color tokens. Over 200 is sprawl. Under 15 means hardcoding lives elsewhere.

**Spacing** — should match a single scale (e.g. 4, 8, 12, 16, 24, 32, 48, 64, 96). Find off-scale values in code. Each is a fix.

**Type** — extract actual font-sizes used across the codebase. Should match a modular scale. Find orphans.

### Step 3: Validate
- **Contrast pairs** — every (text-token, bg-token) pair in actual UI use. Validate WCAG 2.2 AA (4.5:1 body, 3:1 large/UI). Use an APCA-aware checker for 2026; fall back to WebAIM luminance ratio.
- **Dark mode** — every primitive has a dark counterpart; every contrast pair still validates in dark mode.
- **Color-blind** — simulate Deuteranopia / Protanopia / Tritanopia for status colors (success / warning / error / info).
- **Reduced motion** — system has a `prefers-reduced-motion` token or pattern; non-essential animations are gated.

### Step 4: Component inventory
For each visible component family (button, input, card, modal):

- Count distinct visual variants **actually used** (not just defined)
- Identify near-duplicates — visually similar but technically separate
- Find the implementation: tokenized? CSS-in-JS? CSS modules? Hardcoded?

Heuristic: a button should have ≤4 visual variants × ≤3 sizes × ≤2 state-presets. More = sprawl. Less = under-served.

### Step 5: Figma ↔ code parity
Build a parity matrix:

| Token name | Figma value | Code value | Match? |
|---|---|---|---|
| color/text/primary | #1a1a1a | rgb(26, 26, 26) | yes |
| color/surface/raised | #ffffff | #fafafa | no — drift |

Distinguish intentional Figma-only tokens (proposed for next release) from accidental drift.

## Common findings and what they mean

- **30+ shades of gray in the codebase** — designers were eyeballing, not picking from palette. Consolidate to a 10-step gray scale.
- **`padding: 13px` somewhere** — off-scale spacing. Fix to nearest scale value. If 13 was intentional, the scale is wrong, not the value.
- **Brand color hardcoded as `#0066CC` in 47 files** — token exists but wasn't adopted. Run a codemod.
- **Two "Button" components with 80% visual overlap** — consolidate, add a variant prop for the diff.
- **Dark mode "works" but contrast fails on 4 pairs** — auto-inverted, didn't validate. Manually re-pick failing pairs.
- **Focus indicators inconsistent** — almost always hardcoded in components, not tokenized. Make it a token.
- **Figma has tokens X, Y, Z that don't exist in code** — proposed work that never landed; either ship them or remove from Figma to stop the drift.

## Output format

```markdown
## Design System Audit — [system name]

### Executive summary
- Sources of truth found: [list]
- Total color tokens: Figma X / Code Y / Drift Z
- Total spacing tokens: scale ratio = X, off-scale violations = Y
- Total type tokens: X sizes, Y weights, license coverage = full/partial
- Components inventoried: X primitives, Y in actual use, Z duplicates
- Critical issues: [count]
- WCAG 2.2 AA pass rate of in-use color pairs: X%

### Critical findings (fix before next release)
1. [Issue] — [Impact] — [Effort estimate in hours]
   - Evidence: [file:line, Figma frame, contrast ratio]
   - Fix: [specific action]

### High priority (this quarter)
[...]

### Medium priority (when convenient)
[...]

### Recommendations
- Token source-of-truth: [recommendation with rationale]
- Component consolidation: [duplicate sets to merge]
- Process: [pre-commit hook, Figma linting plugin, codemod] — prevent re-drift
```

## Output discipline

- **Quantify everything.** "Many color tokens" is useless; "127 color tokens, 38 unused, 12 drift" is actionable.
- **Cite evidence** — file paths, Figma node IDs, exact contrast ratios.
- **Effort estimates in hours**, not "small/medium/large."
- **Distinguish must-fix-for-WCAG** from "I'd design it differently."
- **Recommend remediation processes** (pre-commit lint, Figma plugin), not just one-time fixes.

## Anti-patterns to call out in the audit

- **"We have tokens" — but the codebase still hardcodes.** Tokens unused = no tokens.
- **"Figma is the source of truth" — but designers can't push to it.** Then it's read-only fiction.
- **"Tailwind is our design system."** Tailwind is a delivery mechanism; tokens still need to be authored.
- **"The brand book PDF is the source."** Static PDFs don't propagate; DTCG JSON does.
- **Auto-generated dark mode without re-validating contrast.** Looks dark, fails AA.
- **Recommending a tool the team can't maintain.** A maintained 5-color hand-audit beats an unmaintained Stark integration.

## Tooling for auditors

- **Style Dictionary** — DTCG-aware token transformer.
- **Tokens Studio for Figma** — DTCG editor inside Figma.
- **Stark / Able / contrast-grid** — Figma plugins for contrast audit at scale.
- **ESLint plugin for design tokens** — flags hardcoded colors in code.
- **Storybook + a11y addon** — surfaces contrast and keyboard issues at component-doc time.

## When the audit recommends consolidation

If findings point to one-tier sprawl, recommend a three-tier architecture (primitive / semantic / component). The semantic tier is where dark mode, density variants, and brand-A/brand-B switching live — without it, every variant multiplies the surface area to maintain. See [[relatedTo::Design Tokens Architecture]] for the tier model.

## Relations

[[relatedTo::Design Tokens Architecture]]
[[relatedTo::Color Management for Designers]]
[[relatedTo::Typography System Design]]
[[implements::WCAG 2.2 Audit Practice]]
[[uses::Style Dictionary]]
[[uses::Tokens Studio for Figma]]

## References

- W3C Design Tokens Community Group: https://www.w3.org/community/design-tokens/
- WCAG 2.2: https://www.w3.org/TR/WCAG22/
- APCA contrast algorithm (under study for WCAG 3): https://www.myndex.com/APCA/
- Material 3 token architecture: https://m3.material.io/foundations/design-tokens/overview
