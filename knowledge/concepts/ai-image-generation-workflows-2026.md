---
title: AI Image Generation Workflows 2026
type: concept
tags:
- design
- AI
- image-generation
- mid-level-architecture
- ComfyUI
- Flux
- SDXL
- production
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# AI Image Generation Workflows 2026

State of the field for designers using AI image generation in production. As of mid-2026: the toy-prompt era is over; the production-pipeline era is here. Reproducibility, brand consistency, and seed/parameter control distinguish hobbyist from professional output.

## The model landscape

| Model | Released | Strength | Access | Commercial use |
|---|---|---|---|---|
| **Midjourney v7** | 2025 | Aesthetic, painterly, composition | Discord / web (closed) | Allowed on paid plans |
| **Flux.1 (Pro / Dev / Schnell)** | 2024 | Photorealism, anatomy, text rendering | Open weights (Dev / Schnell), API (Pro) | Dev/Schnell open license; Pro per-API ToS |
| **SDXL + community ecosystem** | 2023 | Customization, LoRAs, ControlNet | Open weights | OpenRAIL; check fine-tune licenses |
| **Stable Diffusion 3 / 3.5** | 2024 | Multi-subject, text rendering | Open weights / API | Stability commercial license; check tier |
| **Adobe Firefly** | 2023, ongoing | Commercial-safe training data, Photoshop integration | Adobe subscription | Designed for commercial use |
| **Google Imagen 3** | 2024 | Photorealism, complex scenes | Vertex AI, Gemini | Per Google API ToS |
| **DALL-E 3** | 2023 | Prompt adherence, text in images | ChatGPT / API | Allowed per OpenAI ToS |
| **Ideogram 2.0** | 2024 | Best-in-class text rendering, typography | Web, API | Per Ideogram ToS |

The fast-moving fields: better text rendering (Flux, Ideogram, SD3.5 all push this), longer-context prompts, multi-subject consistency, video extension. The slow-moving fields: hands (improved but still error-prone), small-text legibility, fine logo accuracy.

## Production pipeline (the difference between hobbyist and pro)

A hobbyist generation: prompt → one image → ship if lucky.

A production pipeline:

```
1. Brief         (intent, deliverable count, brand constraints)
2. Reference     (mood board, IP-Adapter references, ControlNet inputs)
3. Prompt draft  (subject + composition + lighting + style + technical)
4. Batch 1       (8 images, varied seeds, base prompt)
5. Diagnose      (which axis is off? composition? style? subject?)
6. Iterate       (fix ONE axis at a time, batch again)
7. Lock seed     (winner found — pin seed, vary minor params)
8. Sister set    (seed ±10, generate close cousins)
9. Final selection
10. Provenance   (record seed, model, prompt, parameters; C2PA where supported)
11. Post-process (upscale, color-grade to brand, composite if needed)
```

Each step has a reason. Skipping is what produces "lucky generation" workflows that don't survive client revisions.

## ComfyUI as the production interface

ComfyUI ([github.com/Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI)) has become the de facto power-user interface as of 2026. Its graph/node model offers:

- **Reproducibility** — exact pipeline saved as JSON, sharable, version-controllable
- **Composability** — chain models, LoRAs, ControlNets, IP-Adapters in arbitrary order
- **Seed and parameter control** — every node exposes its knobs
- **Batch processing** — generate variations programmatically
- **Custom nodes** — large community ecosystem for specialty workflows (face restoration, video, animation, super-resolution)

A typical SDXL workflow has 8-12 nodes. A complex production workflow with IP-Adapter, ControlNet, regional prompting, and upscaling has 30+.

Example flows at [comfyanonymous.github.io/ComfyUI_examples/](https://comfyanonymous.github.io/ComfyUI_examples/) cover txt2img, img2img, inpainting, ControlNet, IP-Adapter, and Flux pipelines.

See `knowledge/tools/comfyui.md` for tool-level reference.

## Conditioning techniques

Plain prompting gets you 70% of the way; conditioning techniques close the gap.

### IP-Adapter — style reference
Feed a reference image; the model conditions on its style. Best lever for "8 images that all look like the same brand." Weight typically 0.4-0.7.

### ControlNet — structural conditioning
Feed a structural input (pose skeleton, depth map, canny edges, segmentation mask). The diffusion respects the structure while filling in style and detail.

- **OpenPose**: human poses, posture matching
- **Canny / Scribble**: line-art and sketch driven
- **Depth**: 3D layout matching, perspective control
- **Segmentation**: color-block region control
- **Tile**: high-resolution detail injection
- **LineArt**: clean line-driven generation

### LoRA — small fine-tunes
Low-Rank Adaptations: 5-200MB add-on weights that shift a base model toward a specific style, character, or concept. Apply at generation time with a weight.

Best for: persistent brand illustration style, consistent character across many images, specific product shoots. Train on 20-50 examples.

### Regional prompting
Apply different prompts to different image regions. Useful for multi-subject scenes where "two characters" gets you a merged blob.

### Img2img with denoise control
Feed an existing image; control how much it changes. `denoise=0.0` = no change; `denoise=1.0` = pure txt2img. 0.4-0.7 = restyle keeping structure.

## Prompt engineering, 2026 edition

Many 2022-era prompt conventions are obsolete on modern models.

**Obsolete cargo cult**:
- "masterpiece, 4k, 8k, ultra-detailed, trending on artstation" — modern Flux / SD3.5 / MJ v6+ don't need this; can actively hurt.
- "(detailed face:1.4), (sharp eyes:1.3)" — heavy emphasis weighting causes artifacts.
- Listing 30 negative tokens — model loses focus; 5-8 specific negatives is better.

**What works in 2026**:
- Specific compositional vocabulary (close-up, three-quarter view, low angle)
- Named lighting (Rembrandt lighting, golden hour, rim light)
- Lens / focal length (35mm, 85mm portrait, shallow depth of field)
- Named palettes ("muted earth tones, ochre and umber")
- Style anchored by reference image or LoRA, not by long adjective lists

## Commercial use considerations

Before shipping AI imagery commercially:

- **Training data licensing**: Firefly is designed commercial-safe. SDXL/Flux base models have ambiguous training data. Midjourney commercial use permitted on paid tiers (check current ToS).
- **Output ownership / copyright**: most services grant users broad rights. The US Copyright Office has held that purely AI-generated images cannot be copyrighted (2023 onward) — human authorship is required. Composite/edit work involving meaningful human contribution may qualify.
- **Recognizable persons / brands**: generating real celebrities or trademarked logos is a separate legal risk, model-independent.
- **Living artist style mimicry**: "in the style of [living artist]" for commercial work is legally murky and ethically charged. Stick to public-domain artists or describe styles generically.
- **AI disclosure**: required by some platforms (Meta ad disclosures), jurisdictions (EU AI Act), and contexts (journalism). Check before shipping.
- **C2PA provenance**: emerging standard for embedded metadata declaring AI origin. Adobe Firefly embeds C2PA by default; expect more platforms to follow.

## Quality gates before delivery

A production-grade AI image should pass:

- [ ] Subject is recognizably what the brief asked for (not a near-miss)
- [ ] Anatomy / hands / fingers correct (or absent from frame)
- [ ] Text in the image is correct (if present) — none of the 1-letter mis-renders
- [ ] Composition obeys intended framing (rule of thirds, focal point placed)
- [ ] Color palette matches brief (not "looks AI-saturated" by default)
- [ ] No watermarks, signatures, weird marks bottom-right (artifacts of training data)
- [ ] No artifacts at the resolution shipped (1:1 zoom check, especially around edges)
- [ ] No reproduction of recognizable people / trademarks unintentionally
- [ ] Seed and parameters recorded for reproducibility

## Anti-patterns

- **"Lucky generation" workflow**: one prompt, one image, ship. Cannot survive a revision request.
- **Negative prompt as kitchen sink**: 30+ tags, model loses focus. 5-8 specific negatives.
- **No seed discipline**: every keeper image's seed should be in a `prompts.md` next to it.
- **Forgetting aspect ratio**: generate at target aspect; cropping a square to 16:9 wastes both pixels and composition.
- **Mimicking living artists by name** for commercial work.
- **Stripping or ignoring C2PA metadata** when downstream needs provenance.
- **No human review for commercial output** — clients/platforms/jurisdictions increasingly require it.

## Relations

[[uses::ComfyUI]]
[[uses::Flux Model]]
[[uses::SDXL]]
[[implements::Production Image Pipeline]]
[[relatedTo::Brand Identity Architect Agent]]
[[relatedTo::AI Image Prompting Skill]]

## References

- ComfyUI repository: https://github.com/Comfy-Org/ComfyUI
- ComfyUI example workflows: https://comfyanonymous.github.io/ComfyUI_examples/
- Adobe Firefly: https://www.adobe.com/products/firefly.html
- Flux model documentation (Black Forest Labs)
- C2PA Content Credentials standard: https://c2pa.org/
- US Copyright Office guidance on AI-generated works (2023+)
