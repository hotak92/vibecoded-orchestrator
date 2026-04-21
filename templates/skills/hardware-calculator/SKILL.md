---
name: hardware-calculator
description: Quick VRAM/RAM calculations, hardware recommendations, feasibility checks for AI models
model: haiku
---

# Hardware Calculator (Haiku)

**Purpose**: Quick VRAM/RAM calculations, hardware recommendations, feasibility checks for AI models.

**Model**: Haiku 4.5

## When to Invoke Autonomously

1. **"Can I run X?"**: User asks if model fits hardware
2. **Hardware Shopping**: "Which GPU for [model]?"
3. **Quick VRAM Check**: Before loading model
4. **Multi-Model Planning**: "Can I run 2 models simultaneously?"
5. **Quantization Math**: "How much VRAM saves Q4 vs Q8?"

## DO NOT invoke for

- Complex architecture decisions (use /architect skill)
- Performance optimization (use /performance-optimizer)
- Already know hardware fits

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

**See**: `examples/calculations.md` for formulas, `examples/gpu-specs.md` for hardware details, `scripts/vram_calculator.py` for automated calculations

## Quick Workflow Reference

**Before calculating**: Search for hardware specs and benchmarks
```bash
.claude/scripts/kg-search search "hardware" --type hardware
```

**For deep research**: Ask user "Use hybrid_search to research [GPU comparison]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Success Metrics

- ✅ VRAM estimates within ±1GB of actual usage
- ✅ GPU recommendations fit user's budget and needs
- ✅ Calculations complete in <2 seconds
- ✅ Users don't run into OOM errors after following recommendations
- ✅ Hardware purchases are successful (not over/under-powered)
