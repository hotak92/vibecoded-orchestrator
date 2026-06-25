---
title: Ollama
type: tool
tags: [local-inference, LLM, llama-cpp, embeddings, tool-calling, REST-API]
created: 2026-03-29T00:00:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# Ollama

Local LLM inference server wrapping llama.cpp with a Go-based REST API. Provides Docker-like model management for running open-weight language models on consumer hardware.

## Architecture

- **Core**: Go server wrapping llama.cpp inference engine
- **Default endpoint**: `http://localhost:11434` (Ollama's own default). The orchestrator's containerized Ollama maps host port `11435` → container `11434`, so on a VCO install the host-facing endpoint is `http://localhost:11435`.
- **GPU backends**: CUDA (NVIDIA), Metal (Apple Silicon), ROCm (AMD)
- **Model format**: GGUF (quantized: Q4_K_M, Q8_0; full-precision: FP16/FP32)
- **Processing model**: Sequential request processing (single-user optimized)

## REST API

| Endpoint | Purpose |
|---|---|
| `/api/generate` | Text completions |
| `/api/chat` | Multi-turn conversations |
| `/api/embeddings` | Vector generation |
| `/api/tags` | List available models |
| `/api/ps` | Running model info |

Streaming JSON responses by default. Structured output via JSON schema supported.

## Model Management

Docker-inspired workflow using `Modelfile` format:
```bash
ollama pull llama3.2:3b       # Download model
ollama create mymodel -f Modelfile  # Custom model
ollama push mymodel            # Share to registry
ollama cp source target        # Alias model name
```

### Key Models (2026)

| Model | Size | Use Case |
|---|---|---|
| qwen3-coder | 30.5B | Code generation, agentic tasks |
| qwen3.5:35b-a3b | 35B (3B active MoE) | General + code with low VRAM |
| llama3.2:3b | 3B | Fast local tasks |
| nomic-embed-text | 137M | Embeddings (768-dim) |
| mxbai-embed-large | 334M | High-quality embeddings (1024-dim) |
| snowflake-arctic-embed2 | 568M | Embeddings (1024-dim, 8192 token context) |

## Capabilities

| Feature | Status |
|---|---|
| Streaming | Supported |
| Tool calling | Supported (llama3.2+, qwen2.5+) |
| Vision/multimodal | Supported (llava, llama3.2-vision) |
| Embeddings | Supported |
| Structured output (JSON) | Supported |
| Anthropic Messages API compat | v0.14+ |
| Cloud model offloading | v0.17.5+ (March 2026) |

**Tool calling reliability**: 14B-32B models recommended for production agents; 32B+ for complex multi-tool scenarios.

## Embedding Integration

Vector databases can use `text2vec-ollama` vectorizer module for automatic embedding generation:
- No API keys required
- Automatic vectorization during ingestion and query
- Supported models: nomic-embed-text, mxbai-embed-large, snowflake-arctic-embed2

**Caveat**: embedders need their context window confirmed before chunking — Ollama silently truncates input beyond the model's effective limit. The orchestrator chunks against per-model token budgets (e.g. ~4k for snowflake-arctic-embed2, ~10k for qwen3-embedding:0.6b) rather than the documented maximum.

## Performance

- **Single-user throughput**: ~62 tok/s for Llama 3.1 8B (Q4_K_M) on modern GPU
- **Embedding speed**: ~50-80 tok/s depending on model size
- **Concurrency**: Sequential processing — no request batching. Under multi-user load, vLLM achieves 16.6x higher throughput via PagedAttention and continuous batching.

## Production Considerations

**Strengths**:
- Zero-cost inference (local compute)
- Sub-5-minute setup, single binary
- Data never leaves local system
- 52M monthly downloads (Q1 2026), de facto CLI tool for local LLM

**Limitations**:
- Sequential processing — no native concurrency or request batching
- No built-in load balancing, Prometheus metrics, or request caching
- Memory scales linearly with concurrent model instances
- No RBAC, audit logs, or compliance certifications
- Quantized models trade accuracy for hardware accessibility
- Model library updated on maintainer schedule (not real-time Hugging Face mirroring)
- Failures manifest as degradation, not crashes — hard to diagnose

**When to use vLLM instead**: Multi-user serving, production throughput requirements, GPU cluster deployments, when request batching and PagedAttention matter.

## Context Window Configuration

```bash
# Environment variable
export OLLAMA_CONTEXT_LENGTH=32768

# Or in Modelfile
PARAMETER num_ctx 32768
```

**Critical**: If Ollama cannot fit the KV cache in VRAM, it silently falls back to a smaller context (often 2048). Verify effective context with `ollama ps`. Community recommendation: 32k-65k for qwen3-coder:30b.

## Links

- [[relatedTo::Weaviate]]
- [[relatedTo::Semantic Search and Text Embeddings]]
- [[uses::llama.cpp]]
