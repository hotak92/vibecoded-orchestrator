---
name: workflow-cost-estimator
description: Calculates the realistic monthly cost envelope for a workflow design including LLM tokens, third-party API spend, compute/queue infrastructure, and per-vendor pricing tiers. Use before committing to an engine or before a workflow ships to production
short_desc: monthly cost envelope for workflow + LLM + infrastructure
keywords: [cost envelope, monthly cost, token budget, vendor pricing, compute cost, "LLM cost", "cost of this workflow", "how much will this cost", "pricing for", "LLM cost estimate", "infra cost"]
model: opus
effort: high
---

# Workflow Cost Estimator (Opus)

**Purpose**: Calculate the realistic monthly cost of a workflow BEFORE it ships, so the team knows what they're committing to. Covers LLM tokens, third-party API spend, workflow-engine cost, queue/storage cost. Outputs an envelope at current volume and at 10x scale, with sensitivity analysis on the biggest line items.

**Model**: Opus 4.7

## Why this skill exists

Automation engineers regularly ship workflows that look fine in dev (10 events/day) and become the company's biggest cloud line item at production scale (10M events/day). The math is straightforward but the inputs are non-obvious — a half-hour cost model up front avoids quarter-million dollar surprises in Q3.

## When to invoke autonomously

Invoke when:
1. **Before engine choice**: comparing Zapier vs. n8n vs. Temporal vs. Inngest at the design's expected volume.
2. **Before launch**: a workflow design exists; need a sanity check on the cost envelope.
3. **After an incident**: bill spike investigation — what step is driving it?
4. **Quarterly review**: project workflows running > 6 months, model their current and growth-projected cost.

**Don't invoke for**:
- Single API call wrappers (no meaningful cost model).
- Internal infra cost (use FinOps tooling).

## Usage

```
/workflow-cost-estimator estimate [workflow design path]
/workflow-cost-estimator compare engines for [volume / requirements]
/workflow-cost-estimator audit [workflow] - find biggest line items
```

## Inputs the skill needs

Required:
1. **Volume**: events per day at launch + projected growth (3, 6, 12 months).
2. **Steps**: each step's nature (LLM call, external API, internal DB write, queue, etc.).
3. **LLM steps**: model, average input tokens, average output tokens per call.
4. **External API steps**: provider, pricing tier, expected calls per workflow.
5. **Engine choice**: workflow engine + pricing model.

Ask for these explicitly if missing. **Don't make up numbers**; a confident wrong estimate is worse than no estimate.

## Cost components

### 1. LLM cost (often dominant in modern workflows)

```
Cost per call = (input_tokens × input_price_per_M) + (output_tokens × output_price_per_M)
Monthly cost = Cost per call × calls per day × 30
```

**Reference prices** (2026-05; verify current via provider docs before quoting):

| Model | $/M input | $/M output | Cached input* |
|---|---|---|---|
| Claude Haiku 4.5 | $0.80 | $4.00 | discounted 90% |
| Claude Opus 4.7 | $3.00 | $15.00 | discounted 90% |
| Claude Opus 4.5 | $15.00 | $75.00 | discounted 90% |
| GPT-4o | $2.50 | $10.00 | discounted 50% |
| GPT-4o-mini | $0.15 | $0.60 | discounted 50% |
| Gemini 2.0 Flash | $0.10 | $0.40 | varies |

*Cached input refers to prompt caching where supported (Anthropic, OpenAI). Materially changes cost if your prompt has a large stable prefix (system prompt + few-shot examples).

**Compute the realistic cost**:
- Use AVERAGE tokens, not minimum.
- Add 20-30% for validation-correction round trips (see `knowledge/concepts/function-calling-reliability-patterns.md`).
- Apply prompt-caching discount IF the prompt has a stable prefix > 1024 tokens.
- For batch APIs: 50% discount but ~24h latency — only model if the workflow tolerates it.

**Batch API note**: Anthropic and OpenAI both offer ~50% discount for batched requests with deferred completion. Use for non-realtime workflows (nightly extraction, etc.).

### 2. Workflow engine cost

| Engine | Pricing model | Sample math (10K runs/day, 5 steps each) |
|---|---|---|
| **Self-hosted Temporal** | Infra cost only | 1 small node ~$50/mo; <$0.001/run amortised |
| **Temporal Cloud** | Per state-transition | ~$0.0001/transition × 5 × 10K × 30 = $150/mo |
| **Self-hosted n8n** | Infra only | 1 small node ~$30/mo + Redis ~$15 = $45/mo |
| **n8n Cloud** | Per workflow execution | $0.0002/exec × 10K × 30 = $60/mo (Starter); breakpoints upward |
| **Inngest** | Per step run | Free up to 50K steps/mo; $20+ per 100K steps |
| **AWS Step Functions** | Standard: $0.025/1000 transitions | 5 × 10K × 30 / 1000 × $0.025 = $37.50/mo |
| **Zapier** | Per "task" (one step) | $0.001-$0.01/task → 5 × 10K × 30 = 1.5M tasks → $$$$ |
| **Make.com** | Per "operation" | Similar to Zapier, slightly cheaper per op |

**Decision pattern**: at <1K runs/day, managed SaaS (Zapier/Make) is fine; at 10K+/day, self-host or use Temporal Cloud/Inngest; at 100K+/day, self-host is almost certainly cheapest.

### 3. Third-party API spend

For each external API call, note the provider's pricing:

| Provider | Pricing model | Sample |
|---|---|---|
| Twilio SMS | $0.0075/SMS US | $75/mo at 10K/mo |
| SendGrid Email | Tiered (40K/mo free; $19.95/100K) | $20/mo at 100K |
| Stripe | 2.9% + $0.30 per transaction | Tied to GMV |
| OpenAI/Anthropic | Per-token (see above) | — |
| Slack API | Free up to 1M msgs/mo | Cap-aware |
| Google Workspace API | Free up to quotas; over → paid | Watch quota |
| HubSpot/Salesforce | Per-seat usually, not per-call | Often "free" within plan |

**Watch for**:
- Per-event provider markups (Twilio per-segment counting, SMS over 160 chars splits).
- Currency conversion fees (Stripe charges 1% on cross-currency).
- Search/AI API per-query pricing (Algolia, Pinecone, OpenAI embeddings).

### 4. Infra cost

| Resource | Pricing (AWS-ish) | Typical |
|---|---|---|
| Compute (small worker) | ~$30-50/mo per t3.small | 1-3 workers per workflow |
| Redis (managed) | ~$15/mo small instance | One per cluster, shared |
| Postgres (managed) | ~$50/mo small DB | Shared |
| Queue (SQS standard) | $0.40 per 1M | Negligible until 100M+/mo |
| Egress | $0.09/GB | Often invisible until it bites |
| Observability (Datadog/etc) | Per host + per million events | Can rival LLM cost |

### 5. Hidden costs

- **Retries**: a workflow with 4-attempt retry policy on 1% failures consumes ~1.04× the base cost. Usually negligible UNLESS the retries hit expensive steps (LLM, paid API).
- **Idempotency-key storage**: Redis lookups cost almost nothing; Postgres rows for audit cost more (~1 KB each × N events × retention).
- **Logs / traces**: at scale, observability is often 10-20% of total infra cost.
- **Dead-letter inspection / replay**: ops engineering time, real but often unbudgeted.

## The deliverable

Produce a markdown report:

```markdown
# Workflow Cost Estimate: {name}

## Volume Assumptions
- Today: {N} events/day
- 3 months: {N×1.5} events/day
- 6 months: {N×3} events/day
- 12 months: {N×10} events/day

## Per-event breakdown (today's volume)

| Step | Type | Cost per event | Notes |
|---|---|---|---|
| Webhook receive | infra | $0.0000001 | nginx + worker |
| Validate signature | compute | $0.0000001 | inline, <1ms |
| Idempotency check | Redis | $0.000001 | 1 GET + 1 SET |
| LLM classification (Haiku) | LLM | $0.00012 | 100 tok in + 50 tok out |
| External API call (HubSpot) | external | $0 | within plan |
| LLM extraction (Sonnet, with 1.2× correction multiplier) | LLM | $0.018 | 5K tok in + 500 tok out × 1.2 |
| DB write | infra | $0.0000001 | Postgres write |
| Slack post | external | $0 | within free tier |
| **Total per event** | | **$0.01813** | |

## Monthly cost at each volume tier

| Volume | LLM | Engine | Third-party | Infra | **Total/month** |
|---|---|---|---|---|---|
| 10K/day | $5,439 | $150 (Temporal Cloud) | $0 | $100 | **$5,689** |
| 100K/day | $54,390 | $1,500 | $20 (SendGrid) | $400 | **$56,310** |
| 1M/day | $543,900 | $15,000 | $200 (SendGrid) | $2,000 | **$561,100** |

## Sensitivity analysis

What moves the bill most?

1. **LLM extraction step (96% of cost)**: dropping from Sonnet to Haiku for routine cases saves ~75% on this line item → $4,080/mo at 10K/day.
2. **Validation-correction multiplier**: tightening prompt + schema to reduce 1.2× to 1.05× saves ~12% → $650/mo.
3. **Engine choice**: Temporal Cloud vs. self-hosted at 10K/day = $150 vs. $50 = small. At 1M/day = $15K vs. $200 = huge — invest in self-hosting.
4. **Prompt caching**: if the system prompt is stable, cached input is ~90% cheaper. Estimated savings: $1,500/mo at 10K/day.

## Recommendations

1. Add a Haiku-first classifier to route easy cases away from Sonnet extraction → $4K/mo savings.
2. Enable prompt caching on the Sonnet step → $1.5K/mo savings.
3. Build out self-hosted Temporal before 100K/day volume → avoid Temporal Cloud bill scaling.
4. Tighten extraction schema to reduce correction loops → $650/mo.
5. Add a "circuit breaker" on the extraction step: if cost-per-day > $250 (50% buffer), alert and degrade to cheaper model.

## Open questions

- Confirm assumed average input size for the extraction step (5K tokens). If realistic 8K, cost rises 60%.
- Confirm correction-loop rate via offline eval — currently estimated at 20%.
- Will the volume actually grow 10× in 12 months, or is that pessimistic? (If realistic 3×, the self-hosting investment can wait.)
```

## Common errors to flag

- **Using minimum tokens instead of average** — model the realistic case, not the lucky one.
- **Forgetting validation/correction multiplier** — easy 10-30% miss.
- **Ignoring prompt caching when applicable** — overstates cost.
- **Pricing Zapier at "task" cost when each workflow has 5 steps** — Zapier charges per step, not per workflow.
- **Forgetting batch-API discount where applicable** — non-realtime extraction can be 50% cheaper.
- **Underestimating retry-on-transient-failure cost** — usually small but verify on expensive steps.
- **Not accounting for backfill / one-time migration runs** — a one-week catch-up can be 10× the steady-state monthly bill.

## Tooling

For ongoing tracking, recommend:
- LLM provider dashboards (Anthropic Console, OpenAI usage) — daily review.
- Self-built `costs.jsonl` (the orchestrator's cost tracker is one example) — per-call cost per workflow_id.
- Cloud cost alerts (AWS Budgets, GCP Budget Alerts) — fire at 50%, 80%, 100% of expected.

## Knowledge graph integration

After estimating, write a project node `knowledge/projects/cost-{workflow}.md` with the assumptions, the estimate, and the date. Re-estimate every 90 days; track drift.

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- Known terms → `kg-search` CLI
- Conceptual → `hybrid_search` MCP
- Relationships → `semantic_graph_search` MCP
- Code by purpose → `search_code_graph` MCP
- Literal strings → Grep

## Success metrics

- Estimate uses average (not minimum) token counts and call rates.
- Validation-correction multiplier applied where LLMs are involved.
- 10× volume scenario modelled — surfaces engine-choice break-even points.
- Sensitivity analysis identifies the top 3 cost drivers.
- Concrete optimisation recommendations with dollar amounts attached.
- Open questions flagged where assumptions could materially move the number.
