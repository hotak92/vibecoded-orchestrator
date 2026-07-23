---
name: hardware-calculator
description: Quick VRAM/RAM calculations, GPU sizing, and feasibility checks for AI models. Use when asked "can I run this model on my GPU", "which GPU do I need for X", "how much VRAM does this take", "can I run two models at once", or "how much VRAM does Q4 vs Q8 save". Not for deep architecture decisions (use /architect) or runtime performance tuning (use /performance-optimizer).
short_desc: VRAM/RAM calc + GPU sizing for AI models
keywords: [VRAM, "GPU memory", "RAM requirements", "model footprint", "GPU recommendation", "which GPU", "GPU sizing", "fit on GPU", "memory requirements", H100, A100, "RTX 4090", "consumer GPU"]
model: haiku
---

# Hardware Calculator (Haiku)

Quick VRAM/RAM calculations, GPU recommendations, and feasibility checks for AI models.

## What This Skill Does

**VRAM Calculations**:
- Calculate model VRAM requirements from parameters + quantization
- Account for context overhead and batch size
- Apply 20% safety margin
- Formula: (params × bytes_per_param × 1.2) + context + batch

**GPU Recommendations**:
- Match budget to appropriate GPU tier ($300-5000+)
- Match VRAM needs to GPU options (8-80GB range)
- Consider price/performance tradeoffs
- Warn about overspending or underpowered options

**Feasibility Checks**:
- Quick yes/no: Will model fit on GPU?
- Account for OS overhead (~2GB)
- Warn if <10% headroom (tight fit, unstable)

**Multi-Model Planning**:
- Calculate combined VRAM for running multiple models
- Suggest offloading strategies if tight
- Account for shared context when applicable

## Quick Workflow Reference

**Before calculating**: search for hardware specs and benchmarks.
```bash
.claude/scripts/kg-search search "hardware" --type hardware
```

**For deep research**: run `hybrid_search("<GPU comparison topic>")` (Weaviate MCP).

## Success Metrics

- ✅ VRAM estimates within ±1GB of actual usage
- ✅ GPU recommendations fit user's budget and needs
- ✅ Calculations complete in <2 seconds
- ✅ Users don't run into OOM errors after following recommendations
- ✅ Hardware purchases are successful (not over/under-powered)
