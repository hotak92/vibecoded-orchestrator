---
title: Qwen3-Coder-30B-A3B
type: model
tags: [AI, LLM, MoE, local-inference, qwen, coding, agentic, ollama]
created: 2026-02-25T21:00:00Z
updated: 2026-04-05T14:34:09Z
status: active
---

# Qwen3-Coder-30B-A3B

Qwen3's dedicated coding MoE model. Strong agentic coding performance at 3.3B active params. Primary local model for Ollama integration.

## Architecture

| Property | Value |
|---|---|
| Total params | 30.5B |
| Active params | 3.3B per token |
| Layers | 48 |
| Experts total | 128 |
| Active experts | 8 routed |
| Context (native) | 262,144 tokens |
| Thinking mode | No |
| Vision | No |

## Ollama

```bash
ollama pull qwen3-coder:30b      # default Q4_K_M ~19 GB
ollama pull qwen3-coder:30b-fp16 # full precision ~61 GB
```

## Recommended Settings (CPU-heavy, GPU busy with training)

**Modelfile** (`qwen3-coder-100k`):
```
FROM qwen3-coder:30b

PARAMETER num_ctx 131072       # 100k+ context
PARAMETER num_gpu 0            # CPU-only
PARAMETER num_thread 16        # physical cores
PARAMETER temperature 0.7
PARAMETER top_p 0.8
PARAMETER top_k 20
PARAMETER repeat_penalty 1.05
```

```bash
OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama create qwen3-coder-100k -f Modelfile
```

**When GPU available** (just change num_gpu):
```
PARAMETER num_gpu 999
```

## llama-bench Defaults

| Param | CPU-only | GPU-active |
|---|---|---|
| n_batch | 2048 | 4096 |
| n_ubatch | 512 | 4096 |
| --cpu-moe | yes | yes (or tensor override) |
| flash_attn | yes | yes |
| cache_type_k | q8_0 | q8_0 |
| ctx | 131072 | 131072 |

```bash
llama-server \
  -m qwen3-coder-30b-a3b-q4_K_M.gguf \
  -c 131072 -ngl 0 --cpu-moe \
  -fa -ctk q8_0 -ctv q8_0 \
  -b 2048 -ub 512 -t 16 \
  --port 8080
```

## RAM Estimate (100k context)

- Model (Q4_K_M): ~19 GB
- KV cache (q8_0, 100k, 48 layers, 4 KV heads GQA): ~6 GB
- Total: **~25 GB RAM** for CPU-only

## Integration

```bash
ANTHROPIC_AUTH_TOKEN=ollama
ANTHROPIC_BASE_URL=http://localhost:11434  # adjust as needed
claude --model qwen3-coder:30b
```

Community recommendation: use 32k–65k context for stable tool calling; 100k+ is possible but degrades quality on complex agentic tasks.

## Links

- [[relatedTo::Qwen3.5-35B-A3B]]
- [[relatedTo::MoE LLM Optimization]]
- [[uses::Ollama]]
