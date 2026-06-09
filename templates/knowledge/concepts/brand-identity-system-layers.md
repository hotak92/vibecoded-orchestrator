---
title: Brand Identity System Layers
type: concept
tags:
- design
- brand
- identity-system
- high-level-plan
- positioning
- logo-system
- DTCG
- design-system
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Brand Identity System Layers

A modern brand identity is a layered system, not a single logo. The layers constrain each other top-down: positioning shapes the mark; the mark shapes the color and type choices; color and type shape grid, photography, voice, and motion. Skipping a layer produces an identity that drifts within 90 days of launch — there's no rule to enforce consistency against.

This concept defines the seven layers and the deliverables for each. Use it as the structural backbone for new brand work or for auditing an existing identity for what's missing.

## Layer 1: Positioning brief

Before any visual decision, capture in writing:

- **Audience** — who they are, what they already know, what they reject.
- **Promise** — one sentence: what changes for the customer.
- **Personality** — 3 adjectives, each with its anti-pair. Example: "confident, not arrogant; precise, not cold; warm, not cute."
- **Competitive landscape** — what 5 peer brands look like; identify the visual whitespace in the category.

Without this layer, every later decision is taste alone — indefensible in front of stakeholders. The positioning brief is the constitution every later layer answers to.

## Layer 2: Logomark system

A modern identity is a **system of marks**, not one mark. Plan for at least:

- **Primary lockup** — mark + wordmark, horizontal.
- **Stacked lockup** — for square placements.
- **Monogram / app-icon** — works at 16px favicon AND 1024px app store.
- **Wordmark-only** — used when context already supplies the mark (top of stationery).
- **Submark / response mark** — a fragment that signals brand presence without the full mark (social avatars, watermarks).

Define **clearspace** as a multiple of an internal measure (e.g. `x = height of the mark's primary counter`), not in pixels. This makes it survive scaling.

Define **minimum size** for each variant — the size below which legibility breaks. Mark, wordmark, and monogram each have their own minimum.

## Layer 3: Color system

Three-tier token architecture — see [[relatedTo::Design Tokens Architecture]] for the canonical pattern:

1. **Primitive palette** — every color the brand owns, no semantic meaning. ~12 hues × 10 steps.
2. **Semantic tokens** — `surface/default`, `text/primary`, `feedback/success`. UI uses semantics, never primitives directly.
3. **Component aliases** (optional) — `button/primary/bg` references a semantic. Useful at scale.

For **every brand color**, specify across spaces:

```
sRGB hex        — web default
sRGB RGB        — generic digital
Display P3      — wide-gamut iOS / modern Mac displays
Adobe RGB       — pro photography / pre-press
CMYK (named profile, e.g. ISO Coated v2) — print
Pantone Coated  — pre-press, fabric, plastic, paint
Pantone Uncoated — slightly different from C
L*a*b*          — device-independent ground truth for cross-medium matching
```

The L*a*b* triplet is how the eye sees the color independent of any device. Use it to verify the others all aim at the same target. See [[relatedTo::Color Management for Designers]] for the model.

## Layer 4: Typography

Typically **one display + one text** family, or a **single superfamily** with multiple optical sizes. See [[relatedTo::Typography System Design]] for the full layer.

Key decisions in this layer:

- **License model** — per-seat, per-pageview, perpetual, web-only, embed rights for apps. License dominates cost; surface it at brief time, not at handoff.
- **Type scale** — modular ratio (1.125, 1.25, 1.333, 1.5, 1.618). Pick one. Generate 7–10 steps. Document line-height per step (tighter on display, looser on text).
- **Pairing test** — render the pair at body size in a long paragraph AND at display size in a headline. Many pairings die at one size and survive at the other.
- **Fallback stack** — every web font needs a system-font fallback that minimizes layout shift (similar x-height, similar metrics).

## Layer 5: Layout and grid

- **Baseline grid** — 4px or 8px. All vertical rhythm snaps to it.
- **Column grid** — 12-col for web flexibility; 6-col for print; document gutter and margin.
- **Spacing scale** — multiples of base (4, 8, 12, 16, 24, 32, 48, 64, 96). The same scale used across print and screen prevents drift.

## Layer 6: Photography, illustration, iconography

Each gets a one-page direction document:

- **Photography** — subject, lighting, color treatment, crop ratios, post-processing (LUT or preset reference).
- **Illustration** — line weight rules, color-palette subset, geometry (organic vs geometric), shading approach.
- **Iconography** — stroke width, corner radius, metaphor library, grid (24px or 16px), filled vs outline variants.

When AI image generation is part of the pipeline, document the prompt anatomy and reference set — see [[relatedTo::AI Image Prompt Anatomy]].

## Layer 7: Voice and motion

- **Voice samples** — write the same idea three ways (formal / brand / casual), keep the brand one. Document tone shifts by context (in-product copy, marketing, legal).
- **Motion principles** — easing curves, durations, signature transitions. See [[relatedTo::Motion Principles for UI]]. Motion in a brand system has 3–5 signature transitions: app-open / route, modal / sheet open, list re-order, confirmation / success, error / shake.

## The applications checklist

A brand system must survive in the wild. Test every application in a launch checklist:

- App icon (16, 32, 48, 512, 1024)
- Favicon
- Social avatars + cover images (LinkedIn, X, Instagram, YouTube)
- Email signature
- Letterhead + business card (print-ready CMYK + PDF/X-1a)
- Presentation deck cover + section dividers
- Web home hero
- **Light AND dark variants**

If the system can't survive these, the system is incomplete.

## Brand audit checklist

When auditing an existing identity, score each:

| Area | Pass criteria |
|---|---|
| Logo legibility at 16px | Wordmark or monogram still readable |
| Clearspace honored | Reviewed across stationery, social, signage |
| Color contrast | Primary text/bg pair passes WCAG 2.2 AA (4.5:1) |
| Color across spaces | sRGB, P3, CMYK, Pantone all documented |
| Type license coverage | Web, print, embed rights all present |
| Type fallback | System fallback minimizes shift |
| Photography consistent | Same lighting/grade across last 10 assets |
| Voice consistent | Last 5 customer-facing copies feel one-brand |
| Light + dark variants | Mark + UI both work in both themes |
| File hygiene | Source files findable, named, no `final_final_v3.psd` |
| Motion has rules | Easing / duration documented, not ad-hoc |
| RTL + multi-lang | Layout system survives right-to-left and longer-word languages |

Each fail becomes a remediation recommendation with effort estimate. See [[relatedTo::Design System Audit Methodology]] for the audit framework.

## Production handoff

A brand handoff produces:

- **DTCG-format token file** (`tokens.json`) — color, spacing, type, motion. Engineering consumes this.
- **Folder structure** — `/brand/logo`, `/brand/type`, `/brand/color`, `/brand/photography`. Predictable, navigable.
- **File-naming convention** — `brand-{layer}-{variant}-{size}.{ext}`. Avoid `final_final_v3`.
- **License tracking table** — per-asset license source, term, renewal date. Type licensing is recurring cost and legal exposure.

## Common failure modes (challenge these)

- **"We just need a logo."** Every logo job is actually a system job. A mark with no rules around it becomes inconsistent in 90 days. Quote: clearspace + lockup variants + color spec + minimum size are non-optional.
- **"Just match competitor X."** Sameness is the death of recall. Identify the visual whitespace in the category before sketching.
- **"Skip the cross-space color values."** Design that looks correct on sRGB monitor and prints magenta on coated stock is a brand failure, not a print-shop failure.
- **"License is the designer's problem."** Type licensing is recurring cost. Surface it at brief time, not at handoff. Per-pageview Monotype licenses have killed budgets.
- **"Voice is marketing's job."** Voice without visual rhythm is half a brand. Visual without voice is the other half. Treat them together.

## Anti-patterns

- One logo, no system — fails on day 1 of real-world use.
- Color in HEX only — breaks at print and on wide-gamut displays.
- Type pairings tested only at one size — collapses at the other.
- "Make it pop" feedback accepted without challenge — push back with positioning.
- Skipping the clearspace formula — guarantees future violations.
- No light/dark variants — locks the brand to one context.

## Relations

[[relatedTo::Design Tokens Architecture]]
[[relatedTo::Color Management for Designers]]
[[relatedTo::Typography System Design]]
[[relatedTo::Motion Principles for UI]]
[[relatedTo::AI Image Prompt Anatomy]]
[[relatedTo::Design System Audit Methodology]]
[[implements::Brand System Practice]]

## References

- W3C DTCG: https://www.w3.org/community/design-tokens/
- Pantone Color Bridge — paper-to-screen conversion lookups
- WCAG 2.2: https://www.w3.org/TR/WCAG22/
- Material 3 brand expression: https://m3.material.io/styles
