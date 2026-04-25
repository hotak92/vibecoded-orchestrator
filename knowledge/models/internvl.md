---
title: InternVL
type: model
tags: [AI, VLM, vision, open-source, multimodal, MMMU, state-of-the-art]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:34:09Z
status: active
---

## Overview

InternVL is a family of open-source multimodal large language models developed by OpenGVLab (Shanghai AI Laboratory). It is consistently among the top-performing open-source VLMs on major benchmarks. The InternVL3 series (released April 2025) achieves SOTA among open-source models on MMMU with InternVL3-78B scoring 72.2.

The series follows a "mini-GPT-4" style architecture: a powerful vision encoder (InternViT) combined with a capable language model (InternLM or Qwen), connected via a dynamic resolution mechanism.

## Model Series

### InternVL2 (2024)
- Sizes: 1B, 2B, 4B, 8B, 26B, 40B, 76B
- Strong performance on OCR, charts, documents
- Used in document understanding evaluation

### InternVL2.5 (Late 2024)
- Improved reasoning and multilingual capabilities
- Better chain-of-thought for visual tasks

### InternVL3 (April 2025)
- Native multimodal pre-training (not just fine-tuning)
- InternVL3-78B: 72.2 on MMMU (SOTA open-source)
- InternVL3-8B: Strong balance of quality/size
- InternVL3.5 (Aug 2025): Further refinements

## Architecture

### InternViT Vision Encoder
- 300M or 6B parameter vision encoder
- Higher-resolution images via dynamic resolution tiling
- Supports images up to 4K resolution through patches

### Language Model
- InternVL2: InternLM2 language model backbone
- InternVL3: Qwen or InternLM3 backbone
- Context length: up to 128K tokens

### Dynamic High Resolution
Tiles images into patches (448×448 each), allows handling very high resolution:
- Default: 4–12 tiles depending on input size
- Max tiles: 12–24 in high-res mode
- Critical for document understanding and chart reading

## Performance (InternVL2-8B)

| Benchmark | Score | Notes |
|---|---|---|
| MMMU | 51.2 | College-level questions |
| DocVQA | 91.6 | Document question answering |
| ChartQA | 83.3 | Chart comprehension |
| TextVQA | 77.4 | Scene text understanding |
| OCRBench | 794 | OCR accuracy |
| MMBench | 81.7 | General multimodal |

## VRAM Requirements

| Model | FP16 | INT4 (AWQ/GPTQ) |
|---|---|---|
| InternVL2-2B | ~5 GB | ~2 GB |
| InternVL2-8B | ~17 GB | ~6 GB |
| InternVL2-26B | ~53 GB | ~17 GB |
| InternVL3-8B | ~18 GB | ~7 GB |

## Usage

```python
from transformers import AutoProcessor, AutoModel
import torch

model = AutoModel.from_pretrained(
    "OpenGVLab/InternVL2-8B",
    torch_dtype=torch.bfloat16,
    device_map="auto",
    trust_remote_code=True
)
processor = AutoProcessor.from_pretrained(
    "OpenGVLab/InternVL2-8B",
    trust_remote_code=True
)

# Inference
pixel_values = processor.preprocess(image)  # Handles dynamic resolution
response = model.chat(
    processor,
    pixel_values,
    "Describe the key information in this document.",
    generation_config={"max_new_tokens": 512}
)
```

## Strengths

- **Top open-source benchmark scores** — consistently near GPT-4V on major benchmarks
- **Dynamic resolution** — handles high-res documents without resizing artifacts
- **Strong OCR** — one of the best open-source models for text-in-image tasks
- **Multi-image** — supports multiple images in a single conversation
- **Video understanding** — InternVL2 variants include video frame processing
- **Full open weights** — Apache 2.0 license (InternVL2.5 and later)

## Weaknesses

- Larger models (26B+) require multi-GPU or quantization on consumer hardware
- Some variants require `trust_remote_code=True` (custom modeling code)
- Less actively maintained documentation than commercial alternatives

## Comparison

| Model | MMMU (8B class) | Strengths |
|---|---|---|
| InternVL3-8B | ~56 | Documents, OCR, charts |
| Qwen2.5-VL-7B | ~58 | Long context, video, agents |
| LLaVA-NeXT-8B | ~41 | Simple conversational VQA |
| Florence-2-large | N/A | Task tokens, lightweight |

## Related Links

[[relatedTo::Vision-Language Models]]
[[relatedTo::VLM Model Selection Guide]]
[[relatedTo::VLM Quantization Compatibility 2026]]
[[relatedTo::Qwen2.5-VL-7B]]
[[relatedTo::Document Understanding Models]]
[[relatedTo::VRAM Management Pattern]]
[[relatedTo::DeepSeek-VL2]]
