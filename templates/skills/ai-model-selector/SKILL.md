---
name: ai-model-selector
description: Quick guidance on choosing self-hosted AI models (LLM/VLM/Embedding) based on task, VRAM budget, cost, and quality requirements, including quantization level and VRAM sizing. Use when picking a model for a new task, checking what fits a VRAM limit, or comparing two models. Not for tasks already committed to a model or using a hosted API (OpenAI/Anthropic).
short_desc: choose LLM/VLM/embedding model by task + VRAM
keywords: ["model selection", "which model", "pick a model", "choose a model", "best model for", Qwen, Llama, Gemma, Mistral, "VRAM budget", "model cost", "model latency"]
model: sonnet
---

# AI Model Selector

Quick guidance on choosing AI models (LLM/VLM/Embedding) based on task, VRAM, cost, and quality requirements.

**When to invoke**:

1. **Starting AI Project**: "Which model should I use for [task]?"
2. **VRAM Constraints**: "What fits in 16GB VRAM?"
3. **Quality vs Speed**: "Need faster inference, which smaller model?"
4. **Multi-Model Choice**: Embedding model, VLM, or LLM for task?
5. **Quantization Decision**: "Should I quantize, and to what level?"

**Do NOT invoke for**:
- Already committed to a specific model
- Using enterprise/API models (not self-hosted)
- Model choice is non-critical (prototyping)

## Decision Tree

```
Need to choose:
├─ LLM for text generation? → Use this skill
├─ VLM for image understanding? → Use this skill
├─ Embedding model for RAG? → Use this skill
├─ Already decided on model? → Don't use this skill
└─ Using API (OpenAI/Anthropic)? → Don't use this skill
```

## Usage

```
/ai-model-selector llm [task] [VRAM limit]
/ai-model-selector vlm [task] [VRAM limit]
/ai-model-selector embedding [use case]
/ai-model-selector compare [model1] vs [model2]
```

## What This Skill Does

### 1. Task-Based Recommendations

Provides model recommendations for:
- **Text Generation (LLMs)**: Code generation, chat, creative writing, instruction following
- **Vision Understanding (VLMs)**: Document OCR, general vision, chart analysis, fast inference
- **Embeddings**: General RAG, code search, multilingual search

For specific model recommendations by task, see [examples/good-use-cases.md](examples/good-use-cases.md).

### 2. VRAM Calculation

Estimates VRAM requirements based on:
- Base formula: `model_params × bytes_per_param × overhead_factor`
- Quantization impact (Q4_K_M ~4GB, Q5_K_M ~5GB per 7B params)
- Context length overhead
- Batch size multipliers

### 3. Quality-Speed-VRAM Tradeoffs

Analyzes tradeoffs across quality tiers:
- **Expert** (70B+): Best quality, 48GB+ VRAM
- **High** (30-34B): Very good, 24GB VRAM
- **Balanced** (7-14B): Good quality, 8-16GB VRAM
- **Fast** (1-3B): Acceptable, 4-6GB VRAM

### 4. Quantization Recommendations

Guidance on quantization levels:
- **Q4_K_M**: Best balance — noticeable VRAM savings over FP16 with minor quality loss (the common default)
- **Q5_K_M**: Higher quality, slightly larger footprint than Q4_K_M
- **Q8_0**: Near-FP16 quality, largest quantized footprint

## Output Format

See [template.md](template.md) for complete model recommendation structure.

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "llm" --type model
```

**For deep research**: `hybrid_search("model selection [task]")` (Weaviate MCP)

**Development env**: Python 3.12, Weaviate on :8081, Ollama on :11435. KG/code-graph scripts under `.claude/scripts/` activate the project venv automatically.

## Integration with Knowledge Graph

After model selection:
1. Document choice in project node
2. Link to model spec node (create if needed)
3. Capture benchmarks and real-world performance
4. Tag with use case and hardware requirements

## Supporting Files

- **Use Cases**: See [examples/good-use-cases.md](examples/good-use-cases.md) and [examples/bad-use-cases.md](examples/bad-use-cases.md)
- **Template**: Use [template.md](template.md) for structured model recommendations

## Success Metrics

This skill is working well if:
- ✅ Recommended model fits VRAM constraints
- ✅ Quality meets user requirements
- ✅ Speed is acceptable for use case
- ✅ User doesn't need to switch models later
- ✅ Quantization recommendations are accurate

