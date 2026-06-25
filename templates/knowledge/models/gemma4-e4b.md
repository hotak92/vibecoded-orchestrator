---
title: Gemma 4 E4B
type: model
tags: [model, llm, vlm, multimodal, ollama, gemma, google, edge, open-source]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
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

- **KG-summary generation fallback**: `generate-kg-summary.py` targets `gemma4:e4b` when the Claude CLI is unavailable and the host is not capable enough for `qwen3.5:9b`. `install.py`'s `select_kg_summary_backend` picks it for GPU hosts with ≥6 GB but <16 GB VRAM, or CPU hosts with ≥12 GB RAM and ≥6 cores. It is the primary local fallback for summarization on mid- and low-power machines.
- **Bundled by `install.py`**: pulled via `ollama pull` on capable hosts (alongside `qwen3.5:9b`) and as the low-power summary model on hosts that cannot run `qwen3.5:9b`; the `low_resource` preset also lists it in its inference-model override.
- **Not exposed as an MCP tool**: there is no Ollama MCP — Claude's built-in vision and reasoning handle vision/chat use cases. Gemma 4 E4B remains available via Ollama's REST API for direct use outside VCO's default stack.

## Why this model

`gemma4:e4b` fills the low-power KG-summary niche: 128K context, multimodal, instruction-tuned, edge-class footprint. From a different model family than Qwen, which is useful as a fallback when a user prefers a non-Qwen stack.

## License

Gemma Terms of Use (Google's open-but-restricted license — permissive for most commercial use but with a use-policy attached). Verify the current terms on the Hugging Face model card before redistribution; this is **not** a standard OSI-approved license.

## Sources

- [Ollama library — gemma4](https://ollama.com/library/gemma4)
- Google DeepMind release notes via the Ollama library README
