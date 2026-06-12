---
title: read_image — Memory-Aware Vision-Model Gating
type: concept
tags: [ollama, vision, vlm, memory, mcp, low-level-implementation, vibecoded-orchestrator]
created: 2026-04-27T18:30:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

# read_image — Memory-Aware Vision-Model Gating

**Availability note (v0.2.11+)**: The Ollama MCP server is no longer part of the default VCO install (PR-14a, v0.2.11). The `read_image` tool described below remains in the Ollama MCP source code (`claude_mcp_servers/ollama_mcp/`) but is not registered in `~/.claude.json` by default; Claude's native vision via the `Read` tool on image paths is the recommended path. The gating logic itself (resize tiers, VRAM thresholds) documented here applies whenever the tool is invoked directly against a running Ollama instance.

The `read_image` MCP tool (Ollama MCP) returns an image as a base64 data URL Claude can see directly. The optional **local description tier** runs a vision model (`qwen3.5:9b` default) on-device. Memory-aware gating probes free VRAM and system RAM at module load and either picks a fitting model, falls back to a smaller installed one, or skips the description with a clear reason. The image-as-base64 path is unchanged — it always returns.

## What it is

Three concrete pieces:

1. **Per-model memory thresholds** — VRAM + RAM floors for each supported vision model.
2. **Tiered resize budget** — `max_total_pixels` is an upper bound; the actual cap is clamped down to 1024² / 720² / 512² / 256² depending on free VRAM (or RAM in CPU mode).
3. **Auto-fallback to a smaller installed model** if the chosen one doesn't fit but a smaller one does.

## Per-model thresholds

q4_K_M quants; floors include KV cache and image-feature activations, not just file size.

| Model | VRAM | RAM | File size |
|---|---|---|---|
| qwen3.5:9b | 7.5 GB | 12 GB | ~6.0 GB |
| qwen3.5:7b | 6.0 GB | 10 GB | ~4.7 GB |
| qwen3.5:4b | 4.0 GB | 7 GB | ~2.6 GB |
| llama3.2-vision:11b | 9.0 GB | 16 GB | ~7.9 GB |
| llama3.2-vision:90b | 64.0 GB | 110 GB | ~55 GB |
| gemma3:4b / gemma4:e4b | 5.0 GB | 8 GB | ~3.3 GB |

Unknown models fall back to a conservative 7.5 GB VRAM / 14 GB RAM (~8B-class).

Sources: [[Qwen3.5]], [[Gemma 4 E4B]], the Ollama library tag pages, and r/LocalLLaMA OOM reports (Llama 3.2-Vision 11B q4 OOMs on 8 GB cards with full KV cache; 10-12 GB recommended).

## How probing works

`probe_capabilities()` runs once at module load:

1. **GPU**: `nvidia-smi --query-gpu=memory.free` (NVIDIA), `system_profiler SPDisplaysDataType` (Apple), then a generic `nvidia-ml-py` fallback. Fails closed: any error → "no GPU".
2. **RAM**: `psutil.virtual_memory().available` (psutil is a hard dep already).
3. **OS-specific fallbacks**: `/proc/meminfo` (Linux), `sysctl hw.memsize` (macOS), `wmic OS get FreePhysicalMemory` (Windows).
4. Caches result; never re-probes during a session.

Probe never raises — every failure path degrades to "no GPU + 8 GB RAM" defaults. Conservative-but-functional.

## Decision flow when describe=True

```
1. Pick model (env OLLAMA_VISION_MODEL > config default qwen3.5:9b)
2. Check VRAM threshold:
     if VRAM >= threshold: use GPU
     elif RAM >= cpu_threshold: use CPU
     else: try smaller installed model
3. If all installed models too big:
     return image_as_base64 with description_skipped_reason
     (Claude still sees the image directly)
4. Resize budget:
     start at max_total_pixels (default 1,048,576 = 1024²)
     clamp down by VRAM tier:
       <4 GB free → 256²  (65,536 px)
       <6 GB     → 512²  (262,144 px)
       <8 GB     → 720²  (518,400 px)
       >=8 GB    → 1024² (1,048,576 px)
     surface as image_budget_clamped_from in response
```

## Auto-fallback

If `qwen3.5:9b` is configured but the host has only 4 GB free VRAM, the tool tries `qwen3.5:7b` → `qwen3.5:4b` → `gemma4:e4b` in order, using whichever is installed (`ollama list` cache). Skips description with a clear reason if none fit.

## Image-as-base64 always returns

The base64 data URL path is independent of vision-model state. Claude's own vision can read the image even when the local description tier is skipped. This is the core invariant — the tool always returns something useful, never just an error.

## Env override

`OLLAMA_VISION_MODEL=qwen3.5:4b` forces a smaller model regardless of available memory. Useful for low-VRAM dev machines or for testing fallback paths.

## Why it matters

**Stability**: a user with an 8 GB GPU running `qwen3.5:9b` would OOM mid-inference (Ollama swaps to CPU + system RAM, hangs the API request for 60+ seconds). Pre-flight gating returns immediately with a clear reason instead of silently hanging.

**Honesty**: when the description is skipped, the response includes `description_skipped_reason="insufficient VRAM (5.2 GB free, need 7.5 GB for qwen3.5:9b)"`. The agent can act on this; a silent skip would let it assume the description failed for an unknown reason.

**Resize tiering**: Claude's vision works on smaller images; pushing 4-megapixel images through it wastes context budget. Clamp-down at low VRAM is graceful degradation that still produces useful output.

## Files

- `claude_mcp_servers/ollama_mcp/server.py` — `probe_capabilities()`, `_pick_vision_model()`, `read_image()`
- `tests/test_ollama_vision_gating.py` — unit tests (capability probe, threshold logic, model fallback, resize-budget tiers, env override, no-crash on insufficient VRAM)
- `CLAUDE.md` — `read_image` documented with the qwen3.5:9b default

## See also

- `docs/CONFIGURATION.md` "Vision (read_image) memory budget"
- [[uses::Ollama]]
- [[Qwen3.5]]
- [[Gemma 4 E4B]]
