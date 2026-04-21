# Good Use Cases - When to Use AI Model Selector

## Scenario 1: Starting New AI Project

**User Request**: "Need a model for document extraction, have RTX 4090 24GB"

**Why Use This Skill**:
- Model choice needed (not decided yet)
- VRAM constraint specified (24GB)
- Clear task (document extraction)
- Self-hosted requirement

**Expected Output**:
- Recommend VLM (Qwen2.5-VL-7B or InternVL2-8B)
- Quantization: Q4_K_M (~8-10GB VRAM)
- Performance estimates
- Alternative options if need more quality/speed

---

## Scenario 2: Embedding Model for Code Search

**User Request**: "Which embedding model for RAG with code repositories?"

**Why Use This Skill**:
- Specialized embedding needed
- Domain-specific (code)
- Model selection critical for retrieval quality

**Expected Output**:
- Primary: jina-embeddings-code (trained on GitHub/Stack Overflow)
- Alternative: nomic-embed-text (general purpose, works well with code)
- Context length considerations (8K tokens)
- VRAM requirements minimal (~1-2GB)

---

## Scenario 3: Need Faster Inference

**User Request**: "Llama-3-8B too slow, need faster alternative maintaining decent quality"

**Why Use This Skill**:
- Optimization request (speed vs quality tradeoff)
- Current baseline established (Llama-3-8B)
- Quality constraints specified (decent, not maximum)

**Expected Output**:
- Primary: Qwen2.5-3B-Instruct (30-40% faster, ~10% quality drop)
- Alternative: Mistral-7B with Q4 quantization (20% faster than Llama)
- Performance benchmarks
- Quantization impact on speed

---

## Scenario 4: Multi-Model Decision (LLM + VLM + Embeddings)

**User Request**: "Build RAG system for technical documentation with diagrams, 32GB VRAM total"

**Why Use This Skill**:
- Multiple model types needed (LLM, VLM, embeddings)
- VRAM budget allocation required
- Coordinated selection for entire system

**Expected Output**:
- Embeddings: snowflake-arctic-embed2 (~1GB)
- VLM: Qwen2.5-VL-7B Q4 (~8GB) for diagram understanding
- LLM: Qwen2.5-14B Q4 (~12GB) for generation
- Total: ~21GB (leaves 11GB buffer)
- Batch processing strategy to stay within limits

---

## Scenario 5: Quantization Decision

**User Request**: "Should I quantize Llama-3-70B? Have 48GB VRAM but concerned about quality loss"

**Why Use This Skill**:
- Quantization tradeoff analysis needed
- Quality vs VRAM balance
- Specific model and hardware context

**Expected Output**:
- FP16: 140GB (won't fit)
- Q8_0: ~70GB (won't fit)
- Q5_K_M: ~45GB (fits! 2-3% quality loss)
- Q4_K_M: ~35GB (comfortable fit, 4-5% quality loss)
- Recommendation: Q5_K_M for best quality that fits
- Benchmark suggestions to validate

---

## Scenario 6: Domain-Specific Model Selection

**User Request**: "Code generation model for Python, need best quality within 16GB VRAM"

**Why Use This Skill**:
- Domain-specific (code generation)
- Quality priority within VRAM constraint
- Language-specific (Python)

**Expected Output**:
- Primary: DeepSeek-Coder-7B-Instruct-v1.5 (~8GB Q4)
- Why: Trained specifically on code, excellent Python performance
- Alternative: Qwen2.5-Coder-7B (~8GB Q4) - more recent, multilingual
- Benchmark: Better than general-purpose Llama-3-8B for code by 15-20%
- Context: 16K tokens (handles large code files)

---

## Scenario 7: Upgrading Existing Model

**User Request**: "Currently using Mistral-7B, can I get better quality without exceeding 16GB VRAM?"

**Why Use This Skill**:
- Upgrade path exploration
- Constraint specified (16GB limit)
- Baseline established (Mistral-7B)

**Expected Output**:
- Yes! Options:
  1. Qwen2.5-14B Q4 (~12GB) - 10-15% better quality
  2. Mixtral-8x7B Q4 (~16GB) - 15-20% better, at VRAM limit
- Tradeoff: Slightly slower inference (2x params)
- Quality benchmarks comparing to Mistral-7B
- Migration considerations

---

## Scenario 8: Multilingual Requirements

**User Request**: "Need LLM for customer support in 20+ languages, 24GB VRAM available"

**Why Use This Skill**:
- Multilingual requirement (specialized need)
- Language coverage critical
- Quality across languages matters

**Expected Output**:
- Primary: Qwen2.5-14B-Instruct (~12GB Q4) - Strong multilingual (100+ languages)
- Alternative: Command-R+ (if can fit 35B Q4 in 24GB)
- Why not: Llama-3 (English-centric, weaker in non-Latin scripts)
- Evaluation: Test in target languages before deployment
- Embedding: multilingual-e5-large for multilingual RAG

---

## Scenario 9: Budget-Constrained (Small VRAM)

**User Request**: "Have only 8GB VRAM (GTX 1080), need LLM for chatbot"

**Why Use This Skill**:
- Severe VRAM constraint (8GB)
- Task still feasible (chatbot = simple generation)
- Need to maximize quality within limits

**Expected Output**:
- Primary: Qwen2.5-3B-Instruct Q4 (~3GB) - Excellent quality for size
- Alternative: Gemma-2-2B Q4 (~2GB) - Smaller, acceptable quality
- Why these: Efficient architectures, strong performance per parameter
- Note: 70B/30B models not feasible, focus on efficient small models
- Offloading: Could offload layers to CPU (slower but fits larger model)

---

## Scenario 10: Vision + Text Pipeline

**User Request**: "Extract tables from PDFs, then answer questions about data. 32GB VRAM"

**Why Use This Skill**:
- Multi-stage pipeline (VLM → LLM)
- VRAM allocation between models
- Coordinated model selection

**Expected Output**:
1. Stage 1 (OCR/Table Extraction): Qwen2.5-VL-7B Q4 (~8GB)
   - Why: Excellent table understanding
   - Runs first, then unload from VRAM
2. Stage 2 (QA): Llama-3-8B Q4 (~8GB)
   - Why: Strong reasoning, doesn't need vision
   - Load after VLM unloaded
- Sequential loading strategy (only one loaded at a time)
- Total peak VRAM: ~10GB (includes context)
- Alternative: Keep both loaded (16GB total) for faster switching

---

## Common Pattern: Always Provide

For all scenarios, the skill should provide:
- ✅ Primary recommendation with justification
- ✅ Alternative options (faster/higher quality)
- ✅ VRAM requirements (quantized and FP16)
- ✅ Performance characteristics (speed, quality tier)
- ✅ Quantization recommendations with quality impact
- ✅ Implementation code snippets (Ollama loading)
- ✅ Evaluation plan (how to validate choice)
- ✅ Fallback strategy (if doesn't work as expected)
