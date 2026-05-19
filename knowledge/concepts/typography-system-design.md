---
title: Typography System Design
type: concept
tags:
- design
- typography
- design-system
- brand
- mid-level-architecture
- modular-scale
- font-licensing
- fallback-stack
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Typography System Design

A typography system is the layer of a brand where licensing cost, accessibility, performance, and visual identity all collide. Picking a beautiful pair without checking the license is how a designer hands the client a six-figure recurring bill. Picking based on aesthetics without testing at body and display sizes is how a pairing collapses on contact with real copy. This concept defines the components of a production typography system: family selection, scale, line-height, weight set, licensing model, fallback stack.

## The two-family pattern

The default modern brand typography is either:

- **One display + one text family** — e.g. a distinctive display face for headlines and a neutral, well-hinted text family for body, UI, and small sizes.
- **A single superfamily with multiple optical sizes** — modern superfamilies (Source Serif 4, IBM Plex, Inter, Recursive) include optical variants tuned for display vs text vs caption sizes. One license, one consistent voice across all sizes.

Going beyond two families is rare and dangerous. Each additional family is another license, another fallback stack to engineer, another consistency surface to police.

## The pairing test

Render the candidate pair (or single family) in two contexts before committing:

1. **Body size** — a long paragraph of real copy (300+ words). Tests readability, rhythm, x-height comfort.
2. **Display size** — a real headline (not "Lorem ipsum") at the largest size you'll use. Tests presence, character, and how the family handles tight tracking.

Many pairings die at one size and survive at the other. A display face that looks elegant in a 72pt headline can be unreadable at 14pt. A text face that's neutral at 16pt can look generic at 60pt.

## Modular scale — pick one ratio

A type scale is a sequence of sizes derived from a single ratio. Common ratios:

| Ratio | Name | Feel |
|---|---|---|
| 1.125 | Major second | Tight, dense — enterprise, data-heavy |
| 1.2 | Minor third | Conservative — corporate, financial |
| 1.25 | Major third | Balanced — most common default |
| 1.333 | Perfect fourth | Confident — editorial, marketing |
| 1.5 | Perfect fifth | Dramatic — display-led brands |
| 1.618 | Golden ratio | Maximum drama — luxury, lifestyle |

Pick one ratio. Generate 7–10 steps. Document each. The scale should be visible in the design tokens as `font-size.xs`, `font-size.sm`, `font-size.base`, etc. — see [[relatedTo::Design Tokens Architecture]] for the DTCG `dimension` and `typography` token types.

## Line-height per step (not a constant)

The same line-height value doesn't work across the scale. Display sizes want tighter line-heights; body and caption sizes want looser.

Rough starting point:

| Size | Line-height (unitless multiplier) |
|---|---|
| Display (32px+) | 1.1–1.25 |
| Headline (20–32px) | 1.25–1.35 |
| Body (16–20px) | 1.4–1.6 |
| Caption / micro (12–14px) | 1.5–1.7 |

Document line-height as part of each scale step in tokens. Don't ship `font-size` without its paired `line-height`.

## Weight set — pick the actual weights, not the family's full range

A family like Inter ships 9 weights. Loading all 9 is performance suicide. Pick the weights actually used in the system:

- **Minimal** — regular (400) + bold (700). Two weights, two files.
- **Standard** — regular (400) + medium (500) + semibold (600) + bold (700). Four weights cover most UI.
- **Expressive** — light (300) + regular (400) + medium (500) + bold (700). For brand-led, display-heavy work.

Document the chosen weight set; treat any future addition as a deliberate change. Web-fonts that aren't used still cost a download.

## Font licensing — the dominating constraint

Type licensing is recurring cost and legal exposure. Surface it at brief time, not at handoff. The license models you'll encounter:

| Model | Example | Cost shape | Watch out for |
|---|---|---|---|
| **Per-seat desktop** | Most legacy foundries | One-time, per-designer | Designers shipping `.otf` to engineers without license — engineers also need seats |
| **Per-pageview** | Monotype's typenetwork | Recurring, scales with traffic | Can balloon for high-traffic sites; budget killer |
| **Perpetual web** | Some boutique foundries, free | One-time | Often capped at pageview count or rendered minutes |
| **App embed** | Separate from web | Per-app or per-install | Many web licenses do NOT cover apps |
| **Google Fonts (SIL OFL or Apache)** | Inter, Roboto, IBM Plex, etc. | Free | Open license; verify the specific font's license text |
| **System fonts** | -apple-system, BlinkMacSystemFont, Segoe UI | Free, no download | Cross-platform appearance differs |
| **Adobe Fonts** | via Creative Cloud subscription | Bundled with CC | Limited app-embed rights |

Track per-asset license in a license table in the brand book:

- Source (foundry, URL)
- License terms (web pageviews, app installs, embed)
- Term (perpetual / annual)
- Renewal date (if applicable)
- Contact for renewal

## Fallback stack — the layout-shift killer

Every web font needs a system-font fallback that minimizes Cumulative Layout Shift (CLS) while the web font loads. Pick fallbacks with **similar x-height and metrics**.

CSS pattern:

```css
:root {
  --font-sans: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI",
               Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-serif: "Source Serif 4", Georgia, "Times New Roman", Times, serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo,
               Monaco, Consolas, monospace;
}
```

For zero-shift fallbacks, use CSS `size-adjust`, `ascent-override`, `descent-override`, `line-gap-override` on a `@font-face` declaration of the fallback:

```css
@font-face {
  font-family: "Inter Fallback";
  src: local("Arial");
  size-adjust: 107%;
  ascent-override: 90%;
  descent-override: 22%;
  line-gap-override: 0%;
}
```

This makes Arial occupy the same vertical space as Inter, so when Inter loads there's no shift. Tools like Fontaine and Capsize generate these declarations automatically.

## Tabular numbers — the dense-table secret

For numeric columns in dense tables, switch to tabular figures so digits occupy the same width and become directly comparable:

```css
.numeric-column {
  font-variant-numeric: tabular-nums;
}
```

Most modern sans-serifs include both proportional and tabular figure sets. This single property has outsized impact on table readability. See [[relatedTo::Information Density Heuristics for Enterprise UX]] for why this matters.

## Variable fonts — when they're worth it

Variable fonts ship one file containing the full weight/width/optical-size axes. Trade-off:

- **Pro**: one file replaces 5–9 static weight files. Smaller total transfer (often), one HTTP request.
- **Pro**: arbitrary intermediate weights (`font-weight: 450`) without separate files.
- **Con**: single file is larger than any one static weight; if you only use 2 weights, two static files may be smaller.
- **Con**: older browsers fall back to regular static fonts.

Rule of thumb: use variable when shipping 4+ weights of the same family; static when 2 weights.

## Web font performance hygiene

- **Subset** to the characters actually used (Latin Basic, Latin Extended, Cyrillic, etc.). Unicode-range subsetting cuts file size dramatically.
- **WOFF2** is the modern format; WOFF as a fallback. Skip TTF/OTF for the web.
- **`font-display: swap`** — show fallback text immediately, swap in web font when loaded. Better than blocking render on font load.
- **Preload critical fonts** in `<head>` for above-the-fold copy.
- **Self-host** when control matters; Google Fonts CDN is fine for most projects.

## Accessibility minimums

- Body text minimum 16px (most screens) for readable comfort. Smaller is acceptable for captions / legal / metadata.
- Line length 45–75 characters for sustained reading. Wider = re-reading lines; narrower = jumpy eye motion.
- Contrast pair (text-color, background-color) passes WCAG 2.2 AA: 4.5:1 for body, 3:1 for large text (18pt+ or 14pt bold+).
- Don't rely on font weight alone to communicate state — pair with color, icon, or label.

## Anti-patterns

- **Picking the pair on Pinterest, then checking the license.** Reverse the order.
- **Loading 9 weights "just in case."** Each is a download cost. Pick the actual weights used.
- **One line-height for the whole scale.** Display sizes want tighter, body sizes want looser.
- **No fallback stack** — first paint shows nothing or system default; layout shifts when font loads.
- **Web font with no `font-display`** — blocks render until font loads.
- **Tabular figures off in numeric tables** — digits don't align, comparison breaks.
- **Ignoring per-pageview cost** at high-traffic sites — license bill exceeds engineering budget.

## Relations

[[relatedTo::Design Tokens Architecture]]
[[relatedTo::Brand Identity System Layers]]
[[relatedTo::Information Density Heuristics for Enterprise UX]]
[[implements::Web Typography Practice]]

## References

- Modular Scale: https://www.modularscale.com/
- Variable fonts intro: https://web.dev/variable-fonts/
- Fontaine (zero-shift fallbacks): https://github.com/unjs/fontaine
- Capsize (line-height math): https://seek-oss.github.io/capsize/
- Google Fonts: https://fonts.google.com/
- WCAG 2.2 (W3C): https://www.w3.org/TR/WCAG22/
- Font subsetting / `font-display`: MDN web docs
