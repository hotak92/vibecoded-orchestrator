---
title: QLoRA
type: concept
tags: [AI, fine-tuning, LLM, quantization, lora, PEFT, memory-efficiency, mid-level-architecture]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:47Z
status: active
---

# QLoRA

## Overview

QLoRA (Quantized Low-Rank Adaptation) is a parameter-efficient fine-tuning method introduced by Dettmers et al. (NeurIPS 2023) that combines 4-bit quantization of the base model with Low-Rank Adaptation (LoRA) adapters trained in 16-bit precision. It enables fine-tuning of very large LLMs (65B+ parameters) on consumer hardware (single GPU with 48GB VRAM).

The key insight: the base model weights are frozen and stored in quantized format (4-bit NormalFloat), while the small LoRA adapter weights are trained in BFloat16. During the forward pass, quantized weights are dequantized on-the-fly.

## Technical Components

### NF4 Quantization (4-bit NormalFloat)
- Novel 4-bit data type optimized for normally distributed weights
- Better than traditional INT4 because LLM weights follow approximately normal distribution
- Values are quantized to 16 discrete levels optimized for this distribution
- Stored in 4 bits per weight; loaded as BFloat16 for compute

### Double Quantization
- Quantizes the quantization constants themselves (second-level quantization)
- Saves ~0.37 bits/parameter on average
- Enables fitting slightly larger models or longer sequences

### Paged Optimizers
- Uses NVIDIA unified memory to page optimizer states to CPU RAM when GPU VRAM is exhausted
- Prevents OOM crashes during gradient checkpointing spikes
- Makes long-sequence training more stable

### LoRA Adapter (r, alpha hyperparameters)
- Small trainable matrices injected into attention layers
- r (rank): 4, 8, 16, 64 common values. Higher r = more capacity, more VRAM
- alpha: scaling factor (often set equal to r or 2×r)
- Only adapter weights (~0.1–1% of parameters) are updated during training

## VRAM Savings

| Model Size | Full Fine-tuning | LoRA (16-bit) | QLoRA (4-bit) |
|---|---|---|---|
| 7B | ~112 GB | ~28 GB | ~8 GB |
| 13B | ~208 GB | ~56 GB | ~14 GB |
| 33B | ~528 GB | ~132 GB | ~24 GB |
| 65B | ~1040 GB | ~260 GB | ~48 GB |

## Implementation with `bitsandbytes` + PEFT

```python
from transformers import AutoModelForCausalLM, BitsAndBytesConfig
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

# 4-bit quantization config
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_use_double_quant=True,
)

# Load quantized base model
model = AutoModelForCausalLM.from_pretrained(
    "meta-llama/Llama-2-7b-hf",
    quantization_config=bnb_config,
    device_map="auto"
)

# Prepare for k-bit training (handles gradient checkpointing)
model = prepare_model_for_kbit_training(model)

# Add LoRA adapters
lora_config = LoraConfig(
    r=16,
    lora_alpha=32,
    target_modules=["q_proj", "v_proj"],  # Attention matrices
    lora_dropout=0.05,
    bias="none",
    task_type="CAUSAL_LM"
)
model = get_peft_model(model, lora_config)

# Train normally with trl.SFTTrainer or HuggingFace Trainer
```

## Hyperparameter Guidance

| Parameter | Recommendation |
|---|---|
| r (rank) | 8–64; start with 16; increase for complex tasks |
| lora_alpha | 2× rank is common; controls scaling |
| target_modules | q_proj + v_proj minimum; add k_proj, o_proj, gate_proj for more |
| batch_size | 1–4 with gradient accumulation (8–32 effective) |
| learning_rate | 2e-4 to 3e-4; higher than full fine-tune |
| epochs | 1–3; watch for overfitting |

## Quality vs. Full Fine-tuning

QLoRA matches full fine-tuning quality (within 1–2% on benchmarks) for:
- Instruction following tasks
- Single-domain specialization
- Chat fine-tuning (RLHF alignment)

Quality may lag for:
- Low-resource languages
- Highly specialized technical domains
- Tasks requiring large changes to model "knowledge"

## Related Techniques

- **LoRA** — adapter without quantization (requires more VRAM)
- **GPTQ** — post-training quantization only (no training)
- **AWQ** — activation-aware quantization (inference-optimized)
- **FSDP + LoRA** — full-precision LoRA with model sharding across GPUs

## Related Links

[[relatedTo::LoRA Fine-Tuning]]
[[relatedTo::Fine-Tuning for Tool Calling]]
[[relatedTo::VRAM Management Pattern]]
[[relatedTo::SD1.5 LoRA and Embeddings Training]]
[[relatedTo::Model Quantization Techniques]]
[[relatedTo::Gradient Checkpointing]]
