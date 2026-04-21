---
name: ai-model-selector
description: Quick guidance on choosing AI models (LLM/VLM/Embedding) based on task, VRAM, cost, and quality requirements
model: sonnet
---

# AI Model Selector (Sonnet)

**Purpose**: Quick guidance on choosing AI models (LLM/VLM/Embedding) based on task, VRAM, cost, and quality requirements.

**Model**: Sonnet 4.5 (balanced reasoning for model comparison)

**When to Invoke Autonomously**:

Use this skill when:
1. **Starting AI Project**: "Which model should I use for [task]?"
2. **VRAM Constraints**: "What fits in 16GB VRAM?"
3. **Quality vs Speed**: "Need faster inference, which smaller model?"
4. **Multi-Model Choice**: Embedding model, VLM, or LLM for task?
5. **Quantization Decision**: "Should I quantize, and to what level?"

**DO NOT invoke for**:
- Already committed to specific model
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
- **Q4_K_M**: Best balance (4-5% quality loss, 40% VRAM savings)
- **Q5_K_M**: High quality (2-3% loss)
- **Q8_0**: Near-FP16 quality (1% loss)

## Output Format

See [template.md](template.md) for complete model recommendation structure.

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "llm" --type models
```

**For deep research**: Ask user "Use hybrid_search to research [model selection]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

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

