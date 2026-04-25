---
title: Qwen3-Coder-Next
type: model
tags: [coding-model, MoE, local-inference, Ollama, benchmark]
created: 2026-03-27T16:00:00Z
updated: 2026-04-05T14:34:09Z
status: active
---

# Qwen3-Coder-Next — 3B Active Parameter Coding Model

**HuggingFace**: [Qwen/Qwen3-Coder-Next](https://huggingface.co/Qwen/Qwen3-Coder-Next)
**Ollama**: `ollama run qwen3-coder-next` (requires v0.15.5+)

## Specs
- 80B total parameters, **3B active per token** (MoE)
- 48 hybrid layers (GatedDeltaNet attention + MoE)
- 512 total experts, 10+1 active per token
- 256K context length
- Trained with RL on 800K verifiable executable tasks

## Benchmarks
| Benchmark | Qwen3-Coder-Next | DeepSeek-V3.2 | Gemini 2.5 Pro |
|---|---|---|---|
| SWE-Bench Verified | **70.6%** | — | 63.8% |
| SWE-Bench Pro | **44.3%** | 40.9% | — |

Beats models 10-20x larger on coding benchmarks.

## Local Requirements
| Variant | Disk | RAM/VRAM | CPU tok/s | GPU tok/s |
|---|---|---|---|---|
| q4_K_M | 52GB | ~46GB | ~7-8 | 17-85 |
| q8_0 | 85GB | ~86GB | impractical | needs 2x GPU |

**Known issue**: llama.cpp CPU path doesn't exploit sparse expert activation — 3-4x slower than theoretical.

## Links

[[relatedTo::Ollama Local LLM Infrastructure]]
[[extends::Qwen Model Family]]
