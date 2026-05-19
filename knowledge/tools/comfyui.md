---
title: ComfyUI
type: tool
tags:
- AI
- image-generation
- ComfyUI
- design
- workflow
- open-source
- production
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# ComfyUI

Node-based graphical interface for image-generation pipelines. The production-grade tool of choice for designers who need reproducibility, complex conditioning (LoRAs, ControlNets, IP-Adapters), and the ability to share a workflow as a JSON file.

## Architecture

- **Engine**: Python, PyTorch
- **Frontend**: Graph editor in the browser (drag-and-drop nodes)
- **Runtime**: Local (CPU/CUDA/MPS) or hosted
- **Workflow format**: JSON — serializable, shareable, version-controllable
- **Default endpoint**: `http://localhost:8188`

## Why designers use it (vs Automatic1111, ForgeUI, online services)

- **Reproducibility**: a workflow JSON exactly captures every node, parameter, and connection. Re-run a client deliverable in 6 months with confidence.
- **Composability**: chain models, LoRAs, ControlNets in arbitrary order. New conditioning technique published this month → drop in a node.
- **Resource efficiency**: only runs the nodes needed; smarter VRAM management than monolithic SD UIs.
- **Community**: large ecosystem of custom nodes (face restoration, video, animation, super-resolution, regional prompting, batch processing).
- **API mode**: can run headless with workflow JSON as input → integrate into asset pipelines.

## Core node categories

- **Loaders**: Checkpoint, LoRA, VAE, ControlNet, IP-Adapter, embedding
- **Conditioning**: CLIP Text Encode (positive/negative), ControlNet Apply, IP-Adapter Apply, Concat / Combine
- **Samplers**: KSampler (basic), KSamplerAdvanced (seed control), custom samplers
- **Latents**: Empty Latent (txt2img), VAE Encode (img2img start), Latent Upscale
- **Image**: Load Image, Save Image, Preview, VAE Decode, Image Composite
- **Math / Logic**: Math nodes for parameter sweeps, conditional execution

## Reference workflows

[ComfyUI examples](https://comfyanonymous.github.io/ComfyUI_examples/) covers:

- Basic txt2img
- img2img with denoise control
- Inpainting (masked editing)
- ControlNet (pose, canny, depth, segmentation)
- IP-Adapter (style reference)
- Upscaling (latent + tiled)
- Flux pipelines (Schnell, Dev)
- SDXL refiner pipelines
- Animation (video frame interpolation)

## Production patterns

### Pattern: Brand-consistent illustration set
1. Load SDXL base + brand LoRA + IP-Adapter with brand-reference image
2. Prompt template with `{subject}` slot
3. Loop over subjects, generate batch of 4 per subject
4. Save to subject-named folders

### Pattern: Headshot generation with consistent identity
1. IP-Adapter with reference photo (subject)
2. ControlNet OpenPose with target pose-reference
3. SDXL with portrait-tuned checkpoint
4. KSampler, seed sweep, pick keeper
5. Optional: Face Restoration node (CodeFormer / GFPGAN) for cleanup

### Pattern: Product mockup with controlled scene
1. ControlNet Depth with rendered 3D depth map (Blender / 3D tool)
2. ControlNet Canny with product silhouette
3. Prompt describing scene
4. SDXL with photography checkpoint
5. Output: product correctly placed in AI-generated scene

### Pattern: Style transfer keeping structure
1. Load source image
2. VAE Encode → latent
3. KSampler with denoise 0.5-0.7 and new style prompt
4. Output: same composition, different aesthetic

## Installation

```bash
# Linux / macOS
git clone https://github.com/Comfy-Org/ComfyUI.git
cd ComfyUI
pip install -r requirements.txt
python main.py

# Windows
# Download portable release from GitHub releases
# Or follow Linux/macOS path in WSL2 / Git Bash
```

GPU recommended: CUDA 12+ NVIDIA card with ≥8GB VRAM for SDXL; ≥12GB for Flux. CPU and Apple Silicon (Metal) are supported but slow.

## ComfyUI Manager

A community-maintained custom-node manager (separate install) that enables one-click installation of community nodes, model downloads, and updates. Highly recommended for production setups.

## Integration possibilities

- **CLI batch mode**: feed workflow JSON + parameter overrides → render headless. Useful for asset-pipeline scripts.
- **API**: HTTP API for workflow submission. Designers' tools can integrate (Figma plugin posting to local ComfyUI is a known pattern).
- **Cloud deployment**: many providers host ComfyUI with managed GPUs (RunPod, Runway, Replicate-style services).

## Anti-patterns

- **Treating ComfyUI as a toy** — its complexity is the cost of production reproducibility. If "lucky generation" is fine, use Midjourney.
- **Not pinning model + node versions** — workflows break when models update. Pin checkpoint hashes for client deliverables.
- **Massive workflows without documentation** — 50-node graphs are unreadable in 6 months. Group nodes, annotate, save a `README.md` with the JSON.
- **Running locally without checkpoints organized** — `models/` folder becomes 200GB swamp. Use subfolders, naming convention, and a model-tracking JSON.

## Relations

[[implements::AI Image Generation Workflows 2026]]
[[uses::Stable Diffusion]]
[[uses::Flux Model]]
[[relatedTo::AI Image Prompting Skill]]
[[relatedTo::Brand Identity Architect Agent]]

## References

- Repository: https://github.com/Comfy-Org/ComfyUI
- Example workflows: https://comfyanonymous.github.io/ComfyUI_examples/
- Community Discord and r/comfyui — node and pattern discovery
- ComfyUI Manager: community plugin for node/model management

## License

ComfyUI itself is GPL-3.0. Model licenses vary — SDXL is OpenRAIL, Flux Dev/Schnell are open, commercial use varies. Always check the specific model card before commercial deployment.
