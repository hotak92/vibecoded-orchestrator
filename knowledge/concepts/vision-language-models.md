---
title: Vision-Language Models
type: concept
tags: [AI, VLM, multimodal, computer-vision, NLP, deep-learning]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:34:02Z
status: active
---

## Overview

Vision-Language Models (VLMs) are neural architectures that learn a shared multimodal representation from image-text pairs, enabling tasks that require understanding both visual and linguistic content simultaneously. They bridge computer vision and natural language processing into unified systems capable of describing images, answering visual questions, grounding text in images, and generating images from descriptions.

## Architecture Patterns

### Core Components

1. **Vision Encoder** — processes images into dense feature vectors. Common choices:
   - CLIP ViT (Vision Transformer) variants — most widely used
   - DaViT (Dual Attention ViT) — used in Florence-2
   - SigLIP — stronger zero-shot classification
   - Custom CNN-ViT hybrids

2. **Language Model Backbone** — generates or encodes text:
   - Decoder-only LLMs (Llama, Qwen, Mistral) — most modern VLMs
   - Encoder-decoder (T5, mBART) — older architectures (Donut, Pix2Struct)

3. **Projection Layer** — bridges vision and language feature spaces:
   - Linear projection (simple, efficient)
   - MLP (Flamingo, LLaVA-style)
   - Q-Former (BLIP-2) — cross-attention query mechanism
   - Resampler — fixed number of visual tokens

### Generation Paradigm

Modern VLMs follow a sequence-to-sequence approach: image tokens are prepended to text tokens, and the LLM generates responses autoregressively. This enables:
- Visual question answering (VQA)
- Image captioning
- Optical character recognition (OCR)
- Document understanding
- Visual grounding and segmentation

## Key Model Families

| Model | Organization | Params | Strengths |
|---|---|---|---|
| Qwen2.5-VL | Alibaba | 3B–72B | Document, video, long context |
| InternVL3 | OpenGVLab | 2B–78B | SOTA open-source on MMMU |
| Florence-2 | Microsoft | 0.2B–0.7B | Unified vision tasks, lightweight |
| SmolVLM | HuggingFace | 0.5B–2B | Edge deployment |
| Pixtral | Mistral | 12B | Strong instruction following |
| MiniCPM-o | ModelBest | 2.6B | Mobile/edge optimized |
| Molmo | Allen AI | 7B–72B | Open weights, strong grounding |

## Task Taxonomy

**Generation Tasks**:
- Image captioning — describe image content
- Visual storytelling — narrative from image sequence
- Image-to-code — UI or chart reconstruction

**Understanding Tasks**:
- VQA — answer questions about images
- Document QA — extract information from scanned docs
- Chart/table understanding — structured data from visuals
- OCR — text extraction from natural scenes

**Grounding Tasks**:
- Referring expression comprehension — locate described region
- Phrase grounding — align text phrases to image regions
- Visual spatial reasoning — relative positions

## Evaluation Benchmarks

- **MMMU** — college-level multi-discipline questions
- **OCRBench** — OCR accuracy across document types
- **DocVQA** — document visual question answering
- **ChartQA** — chart comprehension
- **TextVQA** — scene text understanding
- **POPE** — object hallucination evaluation

## Practical Considerations

### VRAM Requirements (Inference)
- 0.5B–2B: 4–8 GB (runs on consumer GPU)
- 7B–8B: 8–16 GB (RTX 4080 range)
- 13B–14B: 16–24 GB
- 32B+: multi-GPU or quantization required

### Quantization
- INT4/INT8 via bitsandbytes or GGUF reduces VRAM ~50–75%
- Performance degradation varies: OCR tasks degrade faster than captioning
- AWQ and GPTQ are common post-training quantization methods

### Prompt Engineering
- Provide explicit task instructions ("Extract all text from this image")
- Temperature 0.0–0.2 for deterministic extraction tasks
- Higher resolution inputs improve OCR/detail accuracy (most models support dynamic resolution)

## Integration Patterns

```python
# Standard HuggingFace VLM loading
from transformers import AutoProcessor, AutoModelForVision2Seq

processor = AutoProcessor.from_pretrained("Qwen/Qwen2.5-VL-7B-Instruct")
model = AutoModelForVision2Seq.from_pretrained(
    "Qwen/Qwen2.5-VL-7B-Instruct",
    torch_dtype="auto",
    device_map="auto"
)
```

## Related Links

[[relatedTo::VLM Model Selection Guide]]
[[relatedTo::VLM Prompting Best Practices]]
[[relatedTo::VLM Quantization Compatibility 2026]]
[[relatedTo::VRAM Management Pattern]]
[[relatedTo::Model Quantization Techniques]]
[[relatedTo::Qwen2.5-VL-7B]]
[[relatedTo::InternVL]]
[[relatedTo::Florence 2]]
[[relatedTo::SmolVLM-2B]]
