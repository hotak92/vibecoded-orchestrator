---
title: Gemma 4 E4B
type: model
tags: [model, llm, vlm, multimodal, ollama, gemma, google, edge, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-04-27T18:30:00Z
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

## Where the orchestrator uses it

- **Low-VRAM fallback path**: `claude_mcp_servers/ollama_mcp/server.py` lists `gemma4:e4b` alongside `qwen3.5:4b` and `gemma3:4b` as fallback candidates when the host cannot satisfy the default `qwen3.5:9b` requirements. The vision-model selector in `read_image` walks down this list until one fits.
- **Not bundled by `install.py`**: like Qwen3.5, the user pulls it explicitly (`ollama pull gemma4:e4b`) when their machine cannot run the default 9B class.

## Why this model

The orchestrator ships several fallback options because vision-MCP behaviour must degrade rather than fail on low-VRAM machines. Gemma 4 E4B fills a specific niche: native multimodal at 128K context with edge-class footprint, instruction-tuned by default, and from a different model family than Qwen — useful when a user prefers a non-Qwen stack or hits a Qwen-specific bug.

## License

Gemma Terms of Use (Google's open-but-restricted license — permissive for most commercial use but with a use-policy attached). Verify the current terms on the Hugging Face model card before redistribution; this is **not** a standard OSI-approved license.

## Sources

- [Ollama library — gemma4](https://ollama.com/library/gemma4)
- Google DeepMind release notes via the Ollama library README
