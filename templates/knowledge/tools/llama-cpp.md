---
title: llama.cpp
type: tool
tags: [AI, LLM, inference, C++, GGUF, local-LLM, CPU, GPU, open-source]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:34:53Z
status: active
---

## Overview

llama.cpp is an open-source C/C++ library for LLM inference with minimal dependencies. Created in March 2023, it enables efficient LLM inference on a wide range of hardware — from laptops with no GPU to high-end workstations. Co-developed alongside the GGML tensor library. GitHub: `ggml-org/llama.cpp`.

**Key goal**: Run LLMs locally with minimal setup and state-of-the-art performance across diverse hardware.

## GGUF Format

llama.cpp uses the **GGUF** (GPT-Generated Unified Format) binary format — a single-file container that stores:
- Model weights (optionally quantized)
- Tokenizer vocabulary and special tokens
- Architecture metadata
- Quantization parameters

GGUF replaced the older GGML format in August 2023. Models from HuggingFace can be converted using `convert_*.py` scripts in the repository.

## Quantization Schemes

| Format | Bits | Strategy | Notes |
|---|---|---|---|
| Q2_K | ~2.6 | K-quant | Very aggressive, lowest quality |
| Q3_K_M | ~3.4 | K-quant | Good for very low VRAM |
| Q4_0 | 4 | Legacy | Simple block quantization |
| Q4_K_M | ~4.5 | K-quant | Recommended for 4-bit |
| Q5_K_M | ~5.5 | K-quant | Good balance |
| Q6_K | ~6.6 | K-quant | Near-lossless |
| Q8_0 | 8 | Linear | Very close to FP16 |
| F16 | 16 | Float16 | Full precision |

**K-quants**: Per-layer adaptive quantization with block-level scaling — better quality than uniform quantization at same bit count. Applied primarily to large weight matrices; smaller weights may stay at higher precision.

## Hardware Backends

| Backend | Hardware | Status |
|---|---|---|
| CPU | Any x86/ARM with BLAS | Default; SIMD (AVX2/AVX-512/Neon) |
| CUDA | NVIDIA GPUs | Production; fastest |
| Metal | Apple Silicon (M1/M2/M3) | Production; excellent performance |
| Vulkan | AMD/Intel GPUs | Mature as of 2025 |
| OpenCL | Qualcomm Adreno | Added 2024 |
| ROCm | AMD Instinct | Supported 2025 |

**Hybrid CPU+GPU**: Models too large for GPU VRAM can offload N layers to GPU and keep remainder on CPU. Controlled via `--n-gpu-layers` flag.

## Advanced Features

- **Speculative Decoding**: 2–3× throughput improvement using a small draft model
- **Vision-Language Models**: llava.cpp extension for VLM inference
- **Real-time Streaming**: Token-by-token output via callbacks
- **Grammar-constrained Generation**: Force output to match BNF grammar (JSON, etc.)
- **Embeddings**: Embedding generation for semantic search
- **Parallel Inference**: Multiple concurrent sequences (for batching)
- **KV Cache**: Configurable KV cache size; quantized KV cache support (Q8_0/Q4_0)

## Ollama Relationship

**Ollama uses llama.cpp as its inference backend**. Ollama adds:
- Model management (pull, list, delete)
- HTTP API compatible with OpenAI spec
- macOS launchd/systemd service
- Docker container support

## Performance Examples (RTX 4080 Super 16GB)

Approximate token generation speeds (Q4_K_M, 100% GPU offload):
- 7B model: ~80–120 tok/s
- 13B model: ~50–70 tok/s (may not fit fully; depends on context)
- 34B model: ~20–30 tok/s (fits at Q4 or smaller)
- 70B model: Does not fit in 16GB at Q4; requires multi-GPU or CPU offload

## Building

```bash
git clone https://github.com/ggml-org/llama.cpp
mkdir build && cd build

# CUDA build
cmake .. -DGGML_CUDA=ON
cmake --build . --config Release -j$(nproc)

# CPU-only build
cmake ..
cmake --build . --config Release -j$(nproc)
```

## Links

[[relatedTo::Ollama]]
[[relatedTo::Model Quantization]]
[[relatedTo::Model Inference Formats]]
[[relatedTo::VRAM Management Pattern]]
[[relatedTo::Ollama Claude Code Integration]]
