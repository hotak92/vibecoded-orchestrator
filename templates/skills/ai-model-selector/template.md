# Model Recommendation Template

## Model Recommendation: [Task Description]

**Date**: [YYYY-MM-DD]
**Requester**: [Name/Project]

---

## Requirements Analysis

**Task**: [Specific use case - code generation, chat, document OCR, etc.]
**VRAM Available**: [XGB on GPU Y]
**Priority**: [Quality / Speed / Balance / VRAM-constrained]
**Constraints**:
- [Hardware limits]
- [Performance requirements (latency, throughput)]
- [Quality requirements (accuracy, consistency)]
- [Other constraints (multilingual, domain-specific, etc.)]

**Current Setup** (if upgrading):
- Model: [Current model if any]
- Issues: [Why looking for alternative]

---

## Recommended Model

### Primary Choice: [Model Name]

**Specifications**:
- **Parameters**: [X]B
- **VRAM Required**:
  - FP16: [Y]GB
  - Q8_0: [Z]GB
  - Q4_K_M: [W]GB (recommended)
- **Quality Tier**: [Expert 70B+ / High 30-34B / Balanced 7-14B / Fast 1-3B]
- **Speed**: [~X tokens/sec estimated]
- **Context Length**: [8K / 16K / 32K tokens]

**Why This Model?**:
1. [Reason based on task requirements]
2. [Reason based on VRAM constraints]
3. [Reason based on quality needs]
4. [Benchmark performance for this task type]

**Quantization Recommendation**: Q4_K_M
- **VRAM Savings**: [X]GB (from FP16)
- **Quality Impact**: <5% degradation
- **Speed Impact**: 20-30% faster than FP16

**Best For**:
- [Specific strength 1]
- [Specific strength 2]
- [Specific strength 3]

---

## Alternative Options

### Option 2 (Faster): [Smaller Model Name]

**Specifications**:
- Parameters: [X]B
- VRAM (Q4): [Y]GB
- Speed: [~Z tokens/sec]
- Quality: [Tier]

**Tradeoff**:
- ⚡ [X]% faster inference
- 📉 [Y]% lower quality
- 💾 [Z]GB less VRAM

**Use Case**: High throughput applications, acceptable accuracy, VRAM-constrained

---

### Option 3 (Higher Quality): [Larger Model Name]

**Specifications**:
- Parameters: [X]B
- VRAM (Q4): [Y]GB
- Speed: [~Z tokens/sec]
- Quality: [Tier]

**Tradeoff**:
- 📈 [X]% better quality
- 🐢 [Y]% slower inference
- 💾 [Z]GB more VRAM required

**Use Case**: Critical accuracy, offline processing, sufficient VRAM available

---

## Model Comparison Table

| Model | Params | VRAM (Q4) | Speed | Quality | Best For |
|-------|--------|-----------|-------|---------|----------|
| **[Primary]** | XB | YGB | ★★★★☆ | ★★★★★ | [Primary use case] |
| [Alt 1] | XB | YGB | ★★★★★ | ★★★☆☆ | High throughput |
| [Alt 2] | XB | YGB | ★★☆☆☆ | ★★★★★ | Maximum quality |

---

## Implementation Guide

### Loading the Model

**Using Ollama**:
```python
from ollama import chat

response = chat(
    model='[recommended-model]:Q4_K_M',
    messages=[
        {'role': 'system', 'content': 'You are a helpful assistant.'},
        {'role': 'user', 'content': prompt}
    ],
    options={
        'num_ctx': 4096,  # Context length
        'temperature': 0.7,  # Adjust based on task
        'top_p': 0.9,
    }
)

print(response['message']['content'])
```

**Downloading Model**:
```bash
# Pull from Ollama
ollama pull [recommended-model]:Q4_K_M

# Or from Hugging Face
huggingface-cli download [org]/[model-name] --local-dir ./models/

# Load with llama.cpp
./llama-cli -m ./models/[model-name]-Q4_K_M.gguf -n 512 -p "Your prompt here"
```

---

## VRAM Optimization

### If VRAM-Constrained:

1. **Use Q4_K_M quantization** (saves ~40% VRAM)
   ```bash
   ollama pull [model]:Q4_K_M
   ```

2. **Reduce context length** (if not needed)
   ```python
   options={'num_ctx': 2048}  # Instead of 4096
   ```

3. **Offload layers to CPU** (slower but fits)
   ```python
   options={'num_gpu': 20}  # Keep 20 layers on GPU, rest on CPU
   ```

4. **Use smaller model** (see Alternative Option 2)

---

## Performance Tips

### For Speed:
- ✅ Use Q4 quantization (20-30% faster)
- ✅ Batch requests when possible
- ✅ Keep model loaded in memory (don't reload)
- ✅ Use shorter context for simple tasks
- ✅ Consider smaller model if quality acceptable

### For Quality:
- ✅ Use Q5_K_M or Q8_0 quantization
- ✅ Increase temperature for creativity (0.7-0.9)
- ✅ Lower temperature for factual tasks (0.1-0.3)
- ✅ Use larger context if task needs it
- ✅ Few-shot prompting for better results

### For VRAM:
- ✅ Q4_K_M quantization first
- ✅ Reduce context length
- ✅ Offload layers to CPU if needed
- ✅ Unload model between requests (if infrequent)

---

## Evaluation Plan

### Test with Sample Tasks:

1. **Task 1**: [Example task description]
   - **Expected Quality**: [Metric/threshold]
   - **Expected Speed**: [Latency target]

2. **Task 2**: [Example task description]
   - **Expected Quality**: [Metric/threshold]
   - **Expected Speed**: [Latency target]

3. **Task 3**: [Example task description]
   - **Expected Quality**: [Metric/threshold]
   - **Expected Speed**: [Latency target]

### Success Criteria:
- ✅ Quality: [Metric] > [Threshold] (e.g., accuracy >85%, BLEU >0.6)
- ✅ Speed: < [X] seconds per request (e.g., <3s for generation)
- ✅ VRAM: < [Y]GB peak usage (stays within hardware limits)
- ✅ Reliability: Consistent output across similar inputs

### Benchmarking:
```bash
# Speed test
time ollama run [model]:Q4_K_M "Generate unit test for: def add(a, b): return a+b"

# Quality test (compare outputs)
# Run same prompt multiple times, evaluate consistency
```

---

## Fallback Strategy

### If Recommended Model Doesn't Work:

**Issue: Too slow**
- **Try**: [Smaller model name] or increase quantization to Q4
- **Expected**: [X]% faster, [Y]% quality drop

**Issue: Quality insufficient**
- **Try**: [Larger model name] or reduce quantization to Q5/Q8
- **Expected**: [X]% better quality, [Y] slower, [Z]GB more VRAM

**Issue: VRAM overflow**
- **Try**: More aggressive quantization (Q4 → Q3) or smaller model
- **Also**: Reduce context length, offload layers to CPU

**Issue: Wrong task specialization**
- **Try**: Domain-specific model (e.g., code → DeepSeek-Coder)
- **Also**: Fine-tune base model on your domain

---

## Cost Considerations

### Hardware Requirements:
- **GPU**: [Minimum GPU VRAM needed]
- **RAM**: [Minimum system RAM for context]
- **Storage**: [Model size on disk]

### Inference Costs (Self-Hosted):
- **Electricity**: ~[X]W GPU power × hours of use
- **Hardware Deprecation**: Amortized cost of GPU
- **FREE**: No per-token API costs

### Comparison to API:
- **OpenAI GPT-4**: $[X] per 1M tokens
- **Anthropic Claude**: $[Y] per 1M tokens
- **Self-hosted**: $0 per token (upfront GPU cost)

**Break-even**: Self-hosting cheaper if > [X] tokens/month

---

## Next Steps

1. **Download model**:
   ```bash
   ollama pull [recommended-model]:Q4_K_M
   ```

2. **Test with sample prompts** (use evaluation plan)

3. **Integrate into application** (see implementation guide)

4. **Monitor performance** (VRAM usage, latency, quality)

5. **Document findings** in knowledge graph:
   - Create model node: `knowledge/models/[model-name].md`
   - Link to project node
   - Tag with use case and performance metrics

---

## References

**Model Card**: [Hugging Face URL]
**Benchmarks**: [Link to benchmark results]
**Documentation**: [Official docs if available]

---

## Approval

**Recommended by**: [Name]
**Reviewed by**: [Name]
**Approved**: [Yes/No]
**Date**: [YYYY-MM-DD]
