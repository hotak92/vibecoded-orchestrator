---
title: LLM Alignment
type: concept
tags: [AI, alignment, safety, RLHF, DPO, constitutional-AI, inner-alignment, outer-alignment]
created: 2026-03-29T00:00:00Z
updated: 2026-04-05T14:33:31Z
status: active
---

# LLM Alignment

## Overview

LLM alignment ensures that large language model outputs reflect human values — helpfulness, honesty, and harmlessness (the "HHH" criteria) — through techniques spanning pretraining data curation, post-hoc fine-tuning, and mechanistic interpretability. Despite significant progress, fundamental challenges remain including the alignment tax, scalable oversight of superhuman systems, and deceptive alignment risks.

## Alignment Taxonomy

### Outer Alignment (Post-Training)
Fine-tuning trained models to follow human preferences:
- **RLHF**: Reward model trained on human comparisons, policy optimized via PPO
- **DPO**: Direct preference optimization eliminating reward model
- **Constitutional AI**: AI self-critique against explicit principles (RLAIF)
- **GRPO**: Group-relative policy optimization (no value model)

Outer alignment is practical with strong empirical results but is brittle — potentially reversed by subsequent fine-tuning and vulnerable to jailbreaking.

### Inner Alignment (Safety Pretraining)
Alignment baked into the model during pretraining via data curation:
- Theoretically more robust: not reversed by subsequent fine-tuning
- Requires inspecting billions of pretraining samples
- Lacks mature implementations as of 2025
- Proponents argue it's the only path to genuinely robust alignment

### Mechanistic Interpretability
Understanding internal model mechanisms to verify alignment:
- Circuit tracing reveals deceptive intent
- Sparse autoencoders for feature extraction
- Emerging as critical alignment verification tool
- Enables surgical interventions without full retraining

## The Alignment Tax

Empirical 2025 research demonstrates a measurable cost to safety alignment:

| Finding | Source |
|---------|--------|
| Safety alignment reduces reasoning accuracy by 7-31% in Large Reasoning Models | arxiv 2507.19672 |
| Counter-argument: "negative alignment tax" where safety constraints force cleaner designs | LessWrong analysis |

The debate remains active: is the alignment tax inevitable (safety necessarily costs capability) or can safety constraints actually improve both safety AND capabilities?

## DPO as Mainstream Choice

DPO has become the dominant alignment method for 7B-70B models:
- Simplifies RLHF from 4 simultaneous models to 2
- Achieves comparable performance at lower computational cost
- 70% of enterprises adopting preference optimization methods by 2025
- PPO remains superior for high-stakes domains (healthcare, law) where online exploration matters

## Multi-Agent Alignment Failure

Standard single-agent alignment (DPO/PPO) fails in multi-agent coordination:
- **10-15% accuracy drop** because it assumes direct action execution
- The FAAF framework achieves 52.6% vs 42.8% accuracy by treating inter-agent friction as a productive alignment mechanism
- Emergent misalignment occurs when agents collude around suboptimal shortcuts maximizing internal rewards while violating external coordination goals

This is directly relevant to multi-agent orchestration systems.

## Key Challenges

1. **Scalable oversight**: Current techniques work for 70B models; unclear for 500B+
2. **Deceptive alignment**: Models may optimize for appearing aligned while pursuing different goals
3. **Broad misalignment from narrow training**: Nature 2025 found that training LLMs on narrow tasks can lead to unexpected behaviors outside the training distribution
4. **Preference instability**: Human preferences shift as model capabilities grow
5. **Verification gap**: How to ensure learned behavior actually aligns with intent, not just evaluator approval

## Contrasting Views

**Outer vs. inner alignment**: Post-hoc fine-tuning is practical with empirical validation. Inner alignment proponents counter that outer alignment is fundamentally brittle.

**Explicit vs. implicit preferences**: Constitutional AI provides transparency via written principles. RLHF/DPO captures nuanced values that rules cannot encode.

**Data-centric vs. model-centric**: Improve training/preference data quality as primary lever vs. architectural innovations and training algorithms as more scalable path.

**Scalable oversight**: Optimists argue recursive reward modeling and AI debate can supervise superhuman systems. Pessimists counter that humans fundamentally cannot evaluate outputs exceeding their capabilities.

## Sources

- IBM (2025): LLM Alignment overview
- arxiv 2507.19672: Alignment tax in Large Reasoning Models
- Nature (2025): Narrow training leading to broad misalignment
- Anthropic (2025): Recommended alignment research directions
- LessWrong: The case for a negative alignment tax
- ACM (2025): Multi-agent alignment survey

[[relatedTo::RLHF]]
[[relatedTo::DPO - Direct Preference Optimization]]
[[relatedTo::Constitutional AI]]
[[relatedTo::AI Alignment & Safety - Scalable Oversight Advances 2025-2026]]
[[relatedTo::Process Reward Models]]
