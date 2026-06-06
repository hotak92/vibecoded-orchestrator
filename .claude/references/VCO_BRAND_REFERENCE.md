# VCO Brand & Design Reference

> **Single source of truth** for any UI/visual work in the VibeCoded Orchestrator launcher.
> Extracted from the REAL design system in [`launcher/src/app.css`](../../launcher/src/app.css)
> (the `/* ─── VCT DESIGN SYSTEM ─── */` block) and
> [`launcher/src/lib/components/OrchestratorUpdateProgressModal.svelte`](../../launcher/src/lib/components/OrchestratorUpdateProgressModal.svelte).
>
> **When to read this**: BEFORE generating any mockup, component, modal, loading
> screen, or visual asset for the launcher. Do not invent colors or re-derive the
> palette from individual components — use these tokens verbatim so output is
> brand-consistent on the first try.

---

## 1. Color tokens (verbatim from app.css `@theme`)

| Token | Hex / value | Use |
|---|---|---|
| `--color-bg`        | `#050B1F` | App background (deepest navy) |
| `--color-bg2`       | `#080F28` | Elevated surfaces, form controls |
| `--color-bg3`       | `#0D1735` | Cards, modals |
| `--color-teal`      | `#00BFA6` | **Primary accent** — CTAs, progress, active states |
| `--color-teal-hover`| `#00D4B8` | Teal hover |
| `--color-purple`    | `#7B5FFF` | Secondary accent |
| `--color-purple-hover` | `#8F77FF` | Purple hover |
| `--color-pink`      | `#FF4FA0` | Tertiary accent / errors / highlights |
| `--color-pink-hover`| `#FF6BB3` | Pink hover |
| `--color-text`      | `#F1F5F9` | Primary text |
| `--color-mid`       | `#94A3B8` | Secondary text |
| `--color-muted`     | `#475569` | Tertiary / disabled / hints |
| `--color-card`      | `rgba(255,255,255,0.04)` | Card fill |
| `--color-border`    | `rgba(255,255,255,0.08)` | Borders |
| `--radius-card`     | `16px` | Default card radius (modals use 18-20px) |

**Signature gradient** (`.text-gradient`, used for brand wordmarks):
`linear-gradient(135deg, #00BFA6, #7B5FFF)` → teal-to-purple, clipped to text.

**Hard-shadow accent colors** (for 3D buttons — the darker shade under each):
teal `#009982`, purple `#5a3fd4`, pink `#cc3075`.

---

## 2. Typography

- **Font family**: `'Inter', system-ui, -apple-system, sans-serif`
  (loaded from Google Fonts in `app.html`, weights **300;400;500;600;700;800;900**).
- **Body**: `line-height: 1.6`, antialiased (`-webkit-font-smoothing: antialiased`).
- **Monospace** (for percentages, ports, technical readouts):
  `ui-monospace, "SF Mono", Menlo, Consolas, monospace`.
- Weights in use: 700 for titles/buttons, 600 for labels, 400 body.

---

## 3. Logo

- File: `launcher/static/logo-512.png` (512×512 RGBA) — robot-in-circle mark.
  Reference it as `/logo-512.png` in any in-launcher asset.
- Also available: `logo.png`, `favicon.png`, OS taskbar icons in `src-tauri/icons/`.
- **Brand fill pattern** (the canonical "logo loading" animation): two stacked
  `<img>` of the same logo — base is `grayscale(1) opacity:0.25`, top is full color
  clipped from the top with `clip-path: inset({100-pct}% 0 0 0)`, transition
  `clip-path 320ms cubic-bezier(0.22, 1, 0.36, 1)`. As percentage rises the colored
  logo emerges from the bottom like a rising fluid level. (Origin: Fay-FAB technique,
  reused in `OrchestratorUpdateProgressModal.svelte`.)
- **Do NOT redraw the robot logo by hand in SVG.** Use the real PNG. If a file must
  be standalone, embed the PNG as base64 rather than approximating the mark.

---

## 4. Signature components & effects

These are the recognizable VCO visual idioms. Reuse them, don't reinvent.

**3D buttons** (`.btn-3d` family): chunky tactile buttons with a hard bottom shadow
(`0 4px 0 0 <dark-shade>`) that compresses on `:active` (`translateY(2px) scale(0.97)`)
and lifts on `:hover` (`translateY(-3px) scale(1.02)`) with a colored glow.
Spring easing: `cubic-bezier(0.34, 1.56, 0.64, 1)`. Variants: primary(teal),
secondary(purple), accent(pink), ghost(outline).

**Glass card** (`.glass-card`): `rgba(255,255,255,0.04)` fill, `blur(16px)` backdrop,
`border-radius: 20px`, subtle inset top highlight, lifts + teal glow on hover.

**Pulse ring** (around logos/loaders): absolutely-positioned circle,
`rgba(0,191,166,0.15)`, `animation: pulse 2s ease-out infinite`
(scale 0.95→1.18, opacity 0.6→0).

**Progress fill**: 4-5px track `rgba(255,255,255,0.06)`, teal fill
`rgba(0,191,166,0.8)→#00BFA6`, `transition: width 0.2s ease`. Premium touch: add a
shimmer sweep (`linear-gradient(90deg, transparent, rgba(255,255,255,0.35), transparent)`
translating left→right, 1.6s).

**Background depth**: layered radial gradients on the navy base, e.g.
`radial-gradient(circle at 30% 20%, rgba(0,191,166,0.10), transparent 55%)` +
a faint animated dot/line grid masked toward the center.

---

## 5. Motion vocabulary

| Easing | Value | Feel |
|---|---|---|
| Spring (buttons, cards) | `cubic-bezier(0.34, 1.56, 0.64, 1)` | Bouncy, tactile |
| Fluid rise (logo fill) | `cubic-bezier(0.22, 1, 0.36, 1)` | Smooth ease-out |
| Standard (bars, fades) | `ease` / `ease-out`, 0.2–0.4s | Neutral |

Durations: micro-feedback 80–200ms, transitions 200–400ms, ambient loops 1.4–2.2s.

---

## 6. Overlay / modal recipe (blocking loading screens)

From `OrchestratorUpdateProgressModal.svelte`:
- Overlay: `position:fixed; inset:0; background: rgba(5,11,31,0.92); backdrop-filter: blur(8px)`.
- Card: `max-width:480px; background: rgba(13,23,53,0.95); border:1px solid rgba(0,191,166,0.25); border-radius:18px; box-shadow: 0 30px 80px rgba(0,0,0,0.6)`.
- Stack: logo-fill → title (18px/700) → uppercase teal stage tag (11px, letter-spacing 1.5px) → progress bar → mono percentage + muted message → italic muted hint.

---

## 7. Anti-patterns (what made past mockups look generic)

- ❌ Re-deriving the palette per-component instead of using these tokens.
- ❌ Redrawing the logo by hand → use the real PNG.
- ❌ A single static spinner with no depth → VCO loaders layer pulse + fill + shimmer.
- ❌ Generic "Loading..." text → surface the REAL services/stages
  (Weaviate :8081, Ollama :11435, code-embed :11440, vct-hub :7700, KG sync).
- ❌ Flat backgrounds → always add layered radial-gradient depth on the navy base.
