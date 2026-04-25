---
title: "SmolVLM-2B"
type: model
tags: [model, VLM, image-captioning, lightweight]
created: 2026-01-28T19:00:00Z
updated: 2026-04-05T14:34:10Z
status: active
---

# SmolVLM-2B

Lightweight vision-language model for fast image captioning.

## Specs

**Performance**:
- Parameters: 2B
- VRAM: ~2.5GB (4-bit GGUF)
- Speed: <1s per image (fast tier)
- Quality: Good for simple captions, filtering, batch processing

**Quantization**:
- 4-bit GGUF recommended for speed/memory balance

**Use Cases**:
- Fast image filtering
- Batch image processing
- Web image scanning
- Quick checks before deeper analysis

## Integration

**Backend**: llama.cpp with GGUF quantization

**Download**:
```bash
# HuggingFace model page
# Convert to GGUF if needed
```

**Inference**:
```python
# Via llama.cpp Python bindings
from llama_cpp import Llama
llm = Llama(model_path="smolvlm-2b-q4.gguf", n_ctx=2048, n_gpu_layers=-1)
result = llm.create_chat_completion(
    messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "file://image.jpg"}},
        {"type": "text", "text": "Describe this image briefly."}
    ]}]
)
```

**VRAM Management**:
- Priority: ALWAYS_LOADED (kept warm in memory)
- Rationale: Fast tier always available for quick checks

## Alternatives

- **MiniCPM-V-2.6**: 4B params, slightly better quality but slower

## Links
- [[Image Interpretation MCP Server]]
- [[llama.cpp]]

## Sources
- SmolVLM: https://huggingface.co/HuggingFaceTB/SmolVLM-Instruct
- llama.cpp vision support: https://github.com/ggerganov/llama.cpp
