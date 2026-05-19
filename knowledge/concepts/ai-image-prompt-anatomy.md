---
title: AI Image Prompt Anatomy
type: concept
tags:
- design
- AI
- image-generation
- prompt-engineering
- mid-level-architecture
- Midjourney
- Flux
- SDXL
- production
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# AI Image Prompt Anatomy

A production-grade AI image prompt is structured, not improvised. The structure transfers across modern image models (Midjourney v6+, Flux, SDXL, SD3.5, Firefly, Imagen 3, DALL-E 3, Ideogram) because they all parse natural-language descriptions with similar weighting. This concept defines the universal slot structure, the compositional vocabulary that beats generic adjectives, the negative-prompt discipline, and the iteration loop that distinguishes a hobbyist "lucky generation" from production output.

For the broader landscape (which model for which job, commercial licensing, conditioning techniques like IP-Adapter / ControlNet / LoRA), see [[relatedTo::AI Image Generation Workflows 2026]].

## Universal prompt slots

A production prompt has these slots, in approximate order of weight. Each slot earns its place; words you don't believe in are noise.

```
[Subject] [Subject details] [Action / pose] [Setting] [Composition / framing]
[Lighting] [Style / medium] [Color palette] [Technical / camera] [Quality tags]
```

Worked example (painterly illustration):

```
A weathered sea captain in his 60s, full white beard, reading a leather-bound
journal at a candlelit desk, three-quarter view, leaning forward, intimate
interior of an old wooden ship cabin, brass instruments and rolled charts
visible, eye-level shot, chest-up framing, warm rim light from the candle on
his left side with deep shadow falling behind, oil painting in the style of
Andrew Wyeth, restrained earth-tone palette of umber, oxblood, parchment cream,
strong tonal contrast, 35mm lens compression
```

Read the prompt as nine cooperating decisions, not a sentence.

## Compositional vocabulary (specific beats generic)

The pattern that beats `cinematic, dramatic, atmospheric` is naming the specific terms.

### Camera framing
Extreme close-up, close-up, medium close-up, medium shot, cowboy shot, full shot, long shot, extreme long shot.

### Camera angle
Eye-level, low angle, high angle, dutch angle, bird's-eye, worm's-eye.

### Lens / camera language (works strongly on photo-tuned models)
24mm wide, 35mm reportage, 50mm natural, 85mm portrait, 135mm compressed. Shallow depth of field, deep depth of field, bokeh, tilt-shift, fisheye, macro.

### Lighting
Three-point lighting, Rembrandt lighting, split lighting, butterfly lighting, rim light, backlight, silhouette. Golden hour, blue hour, harsh midday, overcast diffuse, candlelit, neon, fluorescent. Chiaroscuro, high-key, low-key.

### Composition
Rule of thirds, centered, symmetrical, leading lines, negative space, frame-within-frame. Foreground / midground / background layered.

These named techniques are signal; "dramatic lighting" is noise.

## Color direction by named palette

Naming a palette is far more effective than describing individual colors:

- "Muted earth tones, ochre and umber, with sage accents"
- "High-saturation neon, magenta and cyan, against true black"
- "Monochromatic blue, from icy cyan to navy"
- "Wes Anderson palette: salmon pink, mint, cream, mustard"
- "Bauhaus primaries: red, yellow, blue, on warm white"

Beats `colorful, vibrant, beautiful colors`.

## Aspect ratio as a creative decision

Match the aspect to deployment. Cropping a square to 16:9 wastes both pixels and composition.

| Ratio | Use |
|---|---|
| 1:1 | Square — Instagram post, profile, generic |
| 4:5 | Instagram portrait (more mobile real estate) |
| 9:16 | Reels / Stories / TikTok |
| 16:9 | YouTube, web hero, monitor |
| 3:2 | DSLR / editorial |
| 2:3 | Magazine portrait / poster |
| 21:9 | Cinematic / banner |

Generate at the target aspect ratio from the start.

## Negative prompts — discipline matters

SDXL, Flux, SD3.5 support negative prompts; Midjourney uses `--no`. **Specific** negatives beat shotgun lists.

Common useful negatives:

- Anatomy: `extra fingers, deformed hands, fused fingers, mutated, six fingers`
- Photographic when going realistic: `blurry, low quality, jpeg artifacts, watermark, signature`
- Going realistic: `cartoon, illustration, painting, anime`
- Going painterly: `3d render, photographic`
- Composition: `cropped, out of frame, watermark, text`

**Anti-pattern**: dumping 30+ negative tokens. The model loses focus. Keep negatives at 5–8 entries, specific to the failures you actually observe in your set. If hands are fine, don't negative-prompt them.

## Style anchoring techniques

Plain prompting gets you 70% of the way. For brand-consistent or character-consistent output, use a stronger anchor — see [[relatedTo::AI Image Generation Workflows 2026]] for full coverage of IP-Adapter, ControlNet, and LoRA approaches. Brief summary:

- **IP-Adapter** — reference-image style conditioning. Most controllable lever for "8 images that all look like the same brand." Weight 0.4–0.7.
- **LoRA** — small fine-tune for persistent brand style or consistent character. Train on 20–50 examples.
- **ControlNet** — structural conditioning (pose, depth, canny, segmentation). Diffusion stays inside the lines you provide.
- **Reference + prompt weight** — Midjourney `--sref <url> --sw 100`; SDXL `(subject:1.4)`. Avoid weights >1.5 — they produce artifacts.

## Iteration discipline — the difference between hobbyist and pro

The fundamental workflow is "diagnose one axis at a time." Skipping these steps produces "lucky generation" output that can't survive a revision request.

1. **First batch** — 8 images, varied seeds, base prompt. Read the gap between intent and output.
2. **Identify the failing axis** — composition? style? subject? lighting? Fix ONE axis.
3. **Second batch with the single change** — confirms the change works.
4. **Combine wins** — assemble the prompt that hits all axes.
5. **Lock the winner's seed** — iterate on minor params (lighting words, denoise strength) with the seed pinned.
6. **Sister set** — generate 5–10 close cousins (seed +1, +2, …) for final selection.

## Seed discipline

**Save the seed of every keeper.** Same seed + same prompt + same parameters = same image. Without seed records, you can never re-generate, A/B against tweaks, or hand the result to a colleague for refinement.

Production workflow: every saved image's seed lives in a `prompts.md` next to it (model, prompt, negative prompt, parameters, seed). For ComfyUI users, save the entire workflow JSON — it captures everything including the seed.

## Obsolete cargo cult (2026)

Many 2022-era SD1.5 conventions actively hurt modern models. Stop using:

- `masterpiece, 4k, 8k, ultra-detailed, trending on artstation` — modern Flux / SD3.5 / MJ v6+ don't need these and can be hurt by them.
- Heavy emphasis weighting: `(detailed face:1.4), (sharp eyes:1.3)` — produces artifacts.
- 30-token negative-prompt lists — model loses focus.

What works in 2026: specific compositional vocabulary, named lighting, lens / focal length, named palettes, and style anchored by reference image or LoRA — not by long adjective lists.

## Output template for delivering a prompt

When handing a prompt to a designer or collaborator, structure it so it's reproducible:

```markdown
## Model
[Recommended model + why]

## Prompt
[The actual prompt]

## Negative prompt (if applicable)
[Negatives]

## Parameters
- Aspect ratio: [e.g. 16:9 or 1456x816]
- Steps: [e.g. 30]
- CFG / guidance: [e.g. 6.0 for Flux, 7.0 for SDXL]
- Sampler: [e.g. dpmpp_2m_karras]
- Seed: [if locking; or "random for batch"]

## References
- Style reference image: [path or description]
- ControlNet (if used): [type + source image]
- LoRA (if used): [name + weight]

## Iteration plan
1. Generate batch of 8.
2. Pick winners.
3. Adjust [specific axis] in v2.
4. Lock seed, refine.

## Commercial use note
[Any flags re: training data, mimicry, disclosure]
```

## Failure modes

- **Prompt is a list of adjectives** — adjectives are noise; specific nouns + verbs + named techniques are signal.
- **Negative prompt is everything-and-the-kitchen-sink** — 5–8 specific negatives max.
- **Generating without reference / control when consistency matters** — use IP-Adapter, ControlNet, or LoRA.
- **Forgetting aspect ratio until export** — cropping a square to 16:9 wastes pixels and composition.
- **Not saving seeds** — every keeper image's seed belongs in a `prompts.md` next to it.
- **One generation, ship it** — production work requires the iteration loop above.
- **Mimicking living artists by name** for commercial work — legally murky, ethically charged.

## Relations

[[relatedTo::AI Image Generation Workflows 2026]]
[[relatedTo::ComfyUI]]
[[relatedTo::Brand Identity System Layers]]
[[implements::Production Prompt Discipline]]

## References

- ComfyUI example workflows: https://comfyanonymous.github.io/ComfyUI_examples/
- Midjourney parameter reference: https://docs.midjourney.com/
- Flux model card (Black Forest Labs)
- Stability AI prompt guides for SD3 / SDXL
- Adobe Firefly prompting documentation
