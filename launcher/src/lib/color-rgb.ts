// SPDX-License-Identifier: AGPL-3.0-or-later
//
// P1b (v0.2.75 / B-5): single home for the brand accent colors as RGB
// triplets, for `rgba(<triplet>, α)` consumers (card glows, icon washes).
//
// Before this module, THREE components (routes/+page.svelte,
// routes/store/+page.svelte, lib/components/RightSidebar.svelte) each
// carried a local `getColorRgb` returning the same three literals. A
// palette change in app.css would silently NOT propagate to those JS
// copies — the exact "no per-component palette re-derivation" invariant
// the brand reference forbids (.claude/references/VCO_BRAND_REFERENCE.md).
//
// This helper reads the `--color-<name>-rgb` custom property from
// app.css at runtime via getComputedStyle, so a palette change in ONE
// place (app.css) flows to every glow/icon. The baked triplets are the
// SSR/test fallback in ONE place (no runtime CSSOM in a node/SSR
// context) and are kept in lockstep with the tokens in app.css.

/** The brand accent colors that carry an RGB-triplet token in app.css. */
export type BrandColor = 'teal' | 'purple' | 'pink';

/**
 * Baked triplets — the SSR/test fallback used when there is no live
 * CSSOM to read `--color-<name>-rgb` from. MUST match the
 * `--color-*-rgb` values in `src/app.css` (and the hex on the sibling
 * lines). A palette change updates both.
 */
const FALLBACK_RGB: Record<BrandColor, string> = {
  teal: '0,191,166',
  purple: '123,95,255',
  pink: '255,79,160',
};

/** Normalize a raw `getComputedStyle` triplet (may carry stray spaces). */
function normalize(raw: string): string {
  return raw
    .split(',')
    .map((p) => p.trim())
    .filter((p) => p.length > 0)
    .join(',');
}

/**
 * Return the `r,g,b` triplet for a brand accent color, suitable for
 * interpolation into `rgba(<triplet>, α)`.
 *
 * At runtime (browser) it reads the `--color-<name>-rgb` custom property
 * defined in `app.css`, so palette changes propagate automatically. In
 * SSR / test (no `document`) it returns the baked fallback triplet.
 *
 * Unknown colors fall back to teal (the primary accent) rather than
 * throwing — callers pass a validated `BrandColor`, but a defensive
 * default keeps a mis-typed string from producing `rgba(undefined, …)`.
 */
export function getColorRgb(color: BrandColor): string {
  const fallback = FALLBACK_RGB[color] ?? FALLBACK_RGB.teal;

  // No live CSSOM (SSR / vitest node env) → baked fallback.
  if (typeof document === 'undefined' || typeof getComputedStyle !== 'function') {
    return fallback;
  }

  try {
    const raw = getComputedStyle(document.documentElement)
      .getPropertyValue(`--color-${color}-rgb`)
      .trim();
    if (raw) {
      return normalize(raw);
    }
  } catch {
    // CSSOM read failed (unusual) — fall through to the baked triplet.
  }
  return fallback;
}
