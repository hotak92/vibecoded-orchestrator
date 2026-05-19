---
title: LLM Workflow Cost Modelling
type: concept
tags:
  - AI
  - LLM
  - cost
  - automation
  - workflow
  - finops
  - integration
  - mid-level-architecture
  - best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# LLM Workflow Cost Modelling

LLM-bearing workflows look fine in development (10 events/day, negligible cost) and become the company's biggest cloud line item at production scale (10M events/day, $50K+/month). The math is straightforward; the inputs are non-obvious. A half-hour cost model up front avoids quarter-million-dollar surprises in Q3.

This is the methodology — which inputs to gather, which multipliers to apply, which optimisations to evaluate. For engine-specific pricing see `[[relatedTo::Workflow Engine Tradeoffs 2026]]`.

## The four cost buckets

Ranked by how often they dominate:

1. **LLM tokens** — usually 60-95% of cost in modern automation workflows.
2. **Third-party API spend** — payments, SMS, email, search, embeddings.
3. **Workflow engine** — per-execution or per-transition fees.
4. **Infra** — compute, queue, DB, observability.

## Step 1: Gather inputs (refuse to make them up)

Required: volume (events/day at launch + 3/6/12 month projection); per-step nature; LLM steps (model + average input/output tokens); external API steps (provider + pricing tier + calls per workflow); engine choice + pricing model.

A confidently-wrong estimate is worse than no estimate — it gets quoted in design reviews. If you don't have the input, flag it as an open question with a sensitivity range.

## Step 2: LLM token cost (the dominant term)

```
Cost per call = (input_tokens × input_$/M) + (output_tokens × output_$/M)
Monthly      = cost/call × calls/day × 30
```

Reference prices, 2026-05 (verify current at quote time):

| Model | $/M input | $/M output | Cached input* |
|---|---|---|---|
| Claude Haiku 4.5 | $0.80 | $4.00 | ~90% discount |
| Claude Sonnet 4.5 | $3.00 | $15.00 | ~90% discount |
| Claude Opus 4.5 | $15.00 | $75.00 | ~90% discount |
| GPT-4o | $2.50 | $10.00 | ~50% discount |
| GPT-4o-mini | $0.15 | $0.60 | ~50% discount |
| Gemini 2.0 Flash | $0.10 | $0.40 | varies |

*Prompt caching applies above provider thresholds (~1024 tokens stable prefix).

### Three multipliers everyone forgets

**Validation-correction multiplier (1.05-1.30×)**. LLM extraction and tool-use retry on validation failure. Realistic: `base × (1 + correction_rate × avg_rounds)`. Default to 1.20× until eval data refines it. See `[[relatedTo::Function Calling Reliability Patterns]]` and `[[relatedTo::LLM Structured Extraction Pipeline Pattern]]`.

**Prompt caching savings (often 30-70%)**. 4000-token stable prefix + 1000-token variable suffix: without caching $0.015/call; with ~90% discount on cached portion = $0.0042. At 10K calls/day × 30 = $3,240/mo saved. Always model caching when prompt has stable structure — usually the single biggest available optimisation.

**Batch API discount (~50%)**. Anthropic and OpenAI offer batched inference at ~50% off, deferred completion (returns within 24h). Use for nightly extraction, backfills, non-realtime enrichment. Not for user-facing deadlines.

## Step 3: Workflow engine cost

| Engine | Pricing | Sample (10K runs/day × 5 steps, monthly) |
|---|---|---|
| Self-hosted Temporal | Infra only | ~$50/mo |
| Temporal Cloud | Per transition | ~$0.0001 × 5 × 10K × 30 = $150 |
| Self-hosted n8n | Infra only | ~$45 (node + Redis) |
| Inngest | Per step run | Free up to 50K/mo; $20+ per 100K |
| AWS Step Functions | $0.025 / 1K transitions | ~$37.50 |
| Zapier | Per "task" | $0.001-$0.01 × 5 × 10K × 30 = expensive fast |

Thresholds: <1K runs/day → managed SaaS OK; 10K/day → self-host or Temporal Cloud/Inngest competitive; 100K+/day → self-host nearly always cheapest. 10M events/month: Zapier $$$$ vs. self-hosted n8n ~$50/mo. The break-even is sharp.

## Step 4: Third-party API spend

| Provider | Pricing | Sample |
|---|---|---|
| Twilio SMS | $0.0075/SMS US | $75/mo at 10K/mo |
| SendGrid | Tiered (40K free, $19.95/100K) | $20/mo at 100K |
| Stripe | 2.9% + $0.30/txn | Tied to GMV |
| Vector DB (Pinecone, Algolia) | Per-query + storage | Adds up |

Quirks that bite: Twilio per-segment counting (SMS >160 chars splits); Stripe 1% on cross-currency; vector DB per-query AND per-vector-stored; embedding costs (many small calls — batch them).

## Step 5: Infra cost

Compute ~$30-50/mo per worker; Redis ~$15/mo small; Postgres ~$50/mo small; SQS $0.40/1M (negligible until 100M+/mo); egress $0.09/GB; observability per-host + per-million-events (can rival LLM cost at scale).

Hidden costs:
- **Retries**: ~1.04× on 1% failure rate. Negligible UNLESS retries hit LLM/paid API — then 5-10%.
- **Idempotency-key storage**: Redis effectively free; Postgres audit rows add up at retention scale.
- **Logs/traces**: at scale, 10-20% of infra. Most teams don't budget this.
- **Backfill / migration**: a week-long catch-up can be 10× steady-state. Budget it.

## Step 6: Sensitivity analysis

A useful model identifies the top 3 cost drivers + quantifies each plausible optimisation:

```
Total: $5,689/mo (10K/day)
  LLM extraction (Sonnet, ×1.2):   $5,439 (96%)
  Engine (Temporal Cloud):         $150   (2.6%)
  Infra:                           $100   (1.8%)

Top optimisations:
1. Haiku for routine 70%:         ~$3,800/mo savings
2. Prompt caching (4K stable):    ~$1,500/mo savings
3. Self-host Temporal:            ~$100/mo
4. Tighten correction rate 20%→5%: ~$650/mo
```

Order by absolute dollar impact. The 96% LLM dominance means model routing dwarfs engine optimisation.

## Step 7: Model at 1× AND 10× volume

Two things change at scale: engine pricing tiers, and self-host break-even.

```
Volume    LLM      Engine               Infra Total
10K/day   $5,439   $150 (Temporal Cloud) $100  $5,689
100K/day  $54,390  $1,500                $400  $56,310
1M/day    $543,900 $15,000               $2,000 $561,100
```

At 10K/day engine optimisation barely matters; at 1M/day $15K engine bill is real and self-hosting becomes obvious.

## Common modelling errors

- Using minimum tokens instead of average.
- Forgetting validation-correction multiplier (10-30% miss).
- Ignoring prompt caching (overstates cost 30-70%).
- Pricing per-task SaaS as if 1 task = 1 workflow (Zapier per step).
- Forgetting batch API discount.
- Not accounting for backfill / migration runs.
- No 10× projection.
- Conflating list price with negotiated/committed-use pricing.

## Deliverable

```
1. Volume assumptions (today + projection)
2. Per-event cost breakdown (table by step)
3. Monthly cost at each volume tier (1×, 10× minimum)
4. Sensitivity analysis (top 3 movers + savings)
5. Concrete recommendations with dollar amounts
6. Open questions (assumptions that could shift the number)
```

Re-run every 90 days. Token prices drop, prompts grow, volume changes — stale models mislead.

## Links

- [[relatedTo::Workflow Engine Tradeoffs 2026]]
- [[relatedTo::LLM Structured Extraction Pipeline Pattern]]
- [[relatedTo::Function Calling Reliability Patterns]]
- [[relatedTo::Agent Framework Comparison 2026]]
- [[relatedTo::Automation Workflow Design Framework]]

## References

- Anthropic pricing: https://www.anthropic.com/pricing
- OpenAI pricing: https://openai.com/api/pricing/
- Anthropic prompt caching: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- Anthropic batch API: https://docs.anthropic.com/en/docs/build-with-claude/batch-processing
