---
title: Constitutional AI
type: concept
tags: [ai, alignment, safety, rlhf, rlaif, anthropic, training, fine-tuning, mid-level-architecture]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:20Z
status: active
---

# Constitutional AI

## Overview

Constitutional AI (CAI) is Anthropic's method for training harmless AI assistants through
self-supervised alignment — using a written set of principles ("a constitution") rather than
extensive human labeling of harmful outputs. The key innovation is using AI feedback instead of
human feedback to generate preference data for reinforcement learning.

Published: Bai et al., "Constitutional AI: Harmlessness from AI Feedback" (arXiv Dec 15 2022).
Authors include Yuntao Bai, Amanda Askell, Dario Amodei, Sam McCandlish, Jared Kaplan, and ~45
others at Anthropic.

CAI is foundational to the training of Claude models and represents a significant departure from
standard RLHF by replacing the human preference annotation step with model-generated feedback
guided by explicit principles.

## Motivation

Standard RLHF (Reinforcement Learning from Human Feedback) requires human raters to:
1. Generate harmful prompts (red teaming)
2. Label model responses as harmful/harmless

This is expensive, inconsistent, and exposes raters to harmful content at scale. CAI asks: can
we specify desired behavior through a written constitution and let the model evaluate itself?

## The Two-Phase Process

### Phase 1: Supervised Learning from Self-Critique (SL-CAI)

1. **Elicit harmful responses**: Use red-teaming prompts to get the initial model to produce
   potentially harmful outputs (using "helpful-only" prompting that disables safety behaviors).
2. **Self-critique**: Prompt the same model to critique its own response according to a specific
   constitutional principle. Example principle: "Which response is least likely to contain
   harmful or unethical content?"
3. **Self-revision**: Ask the model to revise its response to better comply with the principle.
4. **Iterate**: Repeat the critique-revision loop (typically 1-3 rounds).
5. **Fine-tune**: Collect the final revised responses and fine-tune the model on them via
   supervised learning. This produces the SL-CAI model.

The self-critique uses chain-of-thought reasoning, improving transparency and performance.

### Phase 2: Reinforcement Learning from AI Feedback (RLAIF)

1. **Sample pairs**: Generate pairs of responses from the SL-CAI model for the same prompt.
2. **AI preference labeling**: Use a "feedback model" (a prompted LLM) to evaluate which of the
   two responses is better according to constitutional principles. This produces a dataset of
   AI-labeled preference pairs — no human labels on harmlessness required.
3. **Train preference model (PM)**: Train a reward model on the AI-labeled preference pairs.
4. **RL fine-tuning**: Train the SL-CAI model with RL (PPO) using the preference model as the
   reward signal. This produces the final RL-CAI model.

## The Constitution

The constitution is a human-authored list of principles used to guide critique and revision.
Principles draw from diverse sources including:
- UN Declaration of Human Rights
- Apple's terms of service
- Anthropic's own guidelines

Example principle: "Choose the response that is least likely to contain false information, or
that least reinforces harmful or incorrect beliefs."

The constitution defines what "harmless" means — making the training objective explicit and
auditable. Different constitutions produce models with different behavioral profiles.

## Key Results

- **RL-CAI is virtually never evasive**: Standard RLHF models often refuse any borderline
  request. RL-CAI models engage with difficult queries by explaining objections, rather than
  refusing bluntly.
- **Better resistance to red-teaming**: RL-CAI models show stronger robustness to adversarial
  jailbreak prompts than RLHF-only models.
- **Helpfulness-harmlessness alignment**: CAI makes these properties more compatible rather
  than treating them as a tradeoff.
- **Scalable supervision**: The AI feedback loop reduces the human annotation burden
  significantly, enabling alignment at scale without corresponding linear annotation costs.

## RLAIF vs RLHF

| Aspect | RLHF | RLAIF (CAI) |
|---|---|---|
| Preference labels | Human raters | AI model with constitution |
| Cost | High (human labor) | Low (model inference) |
| Consistency | Variable (inter-rater disagreement) | More consistent per constitution |
| Transparency | Implicit in rater decisions | Explicit in written principles |
| Harmful content exposure | Raters see harmful text | Model processes harmful text |
| Quality | Ground truth human preference | May miss subtle human values |

In practice, production systems (including Claude) combine both: human labels for helpfulness
and factuality, RLAIF for harmlessness at scale.

## Extensions and Influence

- **Collective Constitutional AI (2023)**: Anthropic used public input (Polis platform) to
  crowdsource constitutional principles from diverse groups, moving toward participatory AI
  alignment.
- **Influence on industry**: Google, Meta, and others have adopted RLAIF-like approaches.
- **Claude's training**: All Claude models are trained with CAI-derived methods.
- **Debate and Scalable Oversight**: CAI is related to OpenAI's debate and scalable oversight
  research — all address the question of supervising AI behavior without humans reviewing every
  output.

## Limitations

- Quality of the AI feedback model bounds the quality of alignment
- Constitutional principles require careful human authorship — poorly specified principles
  produce poorly aligned models
- May not capture nuanced cultural variation in what is considered harmful
- The self-critique loop can reinforce biases present in the base model

## Connection to DPO

In CAI's RLAIF pipeline, DPO can serve as a drop-in replacement for the PPO stage, using AI-generated preference labels. This combines CAI's scalable synthetic preference generation with DPO's training simplicity. However, experiments with smaller models show risk of model collapse when combining CAI self-improvement with DPO due to loss of the corrective exploration that PPO provides.

Links: [[relatedTo::RLHF]], [[relatedTo::LLM Alignment]], [[relatedTo::DPO - Direct Preference Optimization]], [[relatedTo::Process Reward Models]], [[relatedTo::Anthropic Claude]], [[relatedTo::AI Alignment & Safety - Scalable Oversight Advances 2025-2026]]
