---
title: Florence-2
type: model
tags: [AI, VLM, vision, microsoft, foundation-model, multi-task, open-source]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:34:08Z
status: active
---

## Overview

Florence-2 is a vision foundation model released by Microsoft in June 2024 (CVPR 2024, arXiv:2311.06242, 524+ citations). It introduces a unified, prompt-based representation that handles a wide variety of computer vision and vision-language tasks through a single model architecture. Released under the MIT license on Hugging Face.

Florence-2 demonstrates that a single small model can handle dozens of CV tasks when trained with the right data and task formulation — challenging the assumption that each task needs a specialized model.

## Architecture

- **Backbone**: DaViT (Dual Attention Vision Transformer) image encoder
- **Text encoder/decoder**: Transformer-based language model
- **Input**: Image + text task prompt
- **Output**: Text (with structured output for detection: `<loc>x1 y1 x2 y2</loc>` tokens)
- **Sizes**: Florence-2-base (232M params), Florence-2-large (771M params)

### Unified Prompt Interface

All tasks are expressed as text-to-text transformations:
```
Input: image + "<CAPTION>"  → Output: "A cat sitting on a red cushion..."
Input: image + "<OD>"       → Output: "<loc>cat</loc><loc>chair</loc>..."
Input: image + "<OCR>"      → Output: "Hello World"
Input: image + "<GROUNDING_CAPTION>A cat" → Output: <loc> bounding boxes
```

## Supported Tasks

| Task | Prompt | Output |
|---|---|---|
| Image captioning | `<CAPTION>` | Short caption |
| Detailed captioning | `<DETAILED_CAPTION>` | Long descriptive text |
| Object detection | `<OD>` | Bounding boxes + labels |
| Dense region captions | `<DENSE_REGION_CAPTION>` | Per-region captions |
| OCR | `<OCR>` | Extracted text |
| OCR with regions | `<OCR_WITH_REGION>` | Text + bounding boxes |
| Caption-to-phrase grounding | `<CAPTION_TO_PHRASE_GROUNDING>` | Visual grounding |
| Referring expression segmentation | `<REFERRING_EXPRESSION_SEGMENTATION>` | Pixel masks |
| Open vocabulary detection | `<OPEN_VOCABULARY_DETECTION>` | Detected objects |
| Region-to-category | `<REGION_TO_CATEGORY>` | Classification |

## Training

Trained on FLD-5B, a massive dataset with:
- 5.4 billion annotations
- 126 million images
- Annotations covering all supported task types in a unified format
- Data augmentation through specialist model pseudo-labeling

## Performance

- Achieves state-of-the-art zero-shot performance on captioning (COCO CIDEr), VQA, and grounding tasks
- Competes with much larger specialist models on individual tasks
- Fine-tuned versions outperform base in specific domains

## VRAM Requirements

| Model | Precision | VRAM |
|---|---|---|
| Florence-2-base | FP32 | ~4GB |
| Florence-2-base | FP16 | ~2GB |
| Florence-2-large | FP16 | ~4GB |

Fits easily on modern GPUs alongside other models.

## Usage with Transformers

```python
from transformers import AutoProcessor, AutoModelForCausalLM
import torch

model = AutoModelForCausalLM.from_pretrained(
    "microsoft/Florence-2-large",
    torch_dtype=torch.float16,
    trust_remote_code=True
).cuda()
processor = AutoProcessor.from_pretrained(
    "microsoft/Florence-2-large",
    trust_remote_code=True
)

inputs = processor(text="<OD>", images=image, return_tensors="pt").to("cuda")
generated_ids = model.generate(
    input_ids=inputs["input_ids"],
    pixel_values=inputs["pixel_values"],
    max_new_tokens=1024,
    num_beams=3
)
result = processor.decode(generated_ids[0], skip_special_tokens=False)
parsed = processor.post_process_generation(result, task="<OD>", image_size=(w, h))
```

## Fine-tuning

Florence-2 is one of the most fine-tunable VLMs for small datasets:
- HuggingFace blog provides a complete fine-tuning guide (June 2024)
- LoRA fine-tuning on the language decoder works well
- Custom tasks can be added by defining new prompt tokens and training examples

## Links

[[relatedTo::Document Understanding Models]]
[[relatedTo::VLM Model Selection Guide]]
[[relatedTo::Image Interpretation AI]]
[[relatedTo::Qwen2.5-VL-7B]]
[[relatedTo::SmolVLM-2B]]
