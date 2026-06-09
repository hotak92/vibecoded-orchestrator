---
title: Gemma 4 E4B
type: model
tags: [model, llm, vlm, multimodal, ollama, gemma, google, edge, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-05-16T03:53:53Z
status: active
---

## Overview

Gemma 4 E4B is the "effective 4B" edge variant of Google DeepMind's Gemma 4 family. The "E" denotes effective parameters — the model is sized for laptop and mobile deployment while still supporting the full Gemma 4 capability surface (text, image input, configurable thinking, native function-calling, native system-prompt role). The family is multimodal across all sizes and uses dense and Mixture-of-Experts architectures depending on tier.

## Footprint

| Tag | File size | Context | Modalities |
|---|---|---|---|
| `gemma4:e2b` | 7.2 GB | 128K | Text, Image |
| `gemma4:e4b` (latest alias) | 9.6 GB | 128K | Text, Image |
| `gemma4:26b` (MoE, 4B active) | 18 GB | 256K | Text, Image |
| `gemma4:31b` | 20 GB | 256K | Text, Image |

For `gemma4:e4b` at q4_K_M the orchestrator's `VISION_MODEL_REQUIREMENTS` table assumes ~5 GB VRAM and ~8 GB system RAM as practical floors with a small KV cache.

## Where the orchestrator uses it (v0.2.11+)

- **KG-summary generation fallback**: `generate-kg-summary.py` targets `gemma4:e4b` on low-power machines (< 24 GB RAM / < 7.5 GB VRAM) when the Claude CLI is unavailable. It is the primary local fallback for summarization workloads.
- **Not bundled by `install.py`**: the user pulls it explicitly (`ollama pull gemma4:e4b`) when their machine cannot run `qwen3.5:9b`.
- **Note (v0.2.11)**: the Ollama MCP (`read_image`, `chat`) was removed. `gemma4:e4b` is no longer used as a vision/chat MCP fallback — Claude's built-in vision and reasoning handle those use cases. Gemma 4 E4B remains available via Ollama for direct REST API access if needed outside of VCO's default stack.

## Why this model

`gemma4:e4b` fills the low-power KG-summary niche: 128K context, multimodal, instruction-tuned, edge-class footprint. From a different model family than Qwen, which is useful as a fallback when a user prefers a non-Qwen stack.

## License

Gemma Terms of Use (Google's open-but-restricted license — permissive for most commercial use but with a use-policy attached). Verify the current terms on the Hugging Face model card before redistribution; this is **not** a standard OSI-approved license.

## Sources

- [Ollama library — gemma4](https://ollama.com/library/gemma4)
- Google DeepMind release notes via the Ollama library README
