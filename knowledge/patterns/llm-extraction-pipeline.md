---
title: LLM Structured Extraction Pipeline Pattern
type: pattern
tags:
  - patterns
  - AI
  - LLM
  - structured-output
  - extraction
  - automation
  - integration
  - mid-level-architecture
  - best-practices
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# LLM Structured Extraction Pipeline Pattern

Turning messy unstructured input (PDFs, emails, transcripts, HTML, support tickets) into validated structured data matching a strict schema. Done well, reliable enough to power production workflows; done poorly, silently produces 80%-correct output and breaks downstream consumers.

Complements `[[relatedTo::Function Calling Reliability Patterns]]` (tool-use focus) — same techniques, different failure modes and design priorities.

## The seven-stage pipeline

```
Input → Preprocess → Build prompt → LLM call → Parse → Validate → [Correct]* → Persist
                                                                │
                                                                └── fail → DLQ + human review
```

Skip any stage and you trade off correctness, cost, or operability.

## 1. Schema design — highest leverage

A loose schema lets the model invent fields, hallucinate values, pass garbage downstream. A strict schema catches failures at validation, not in production.

```python
class InvoiceLineItem(BaseModel):
    model_config = {"extra": "forbid"}      # reject unknown keys
    description: str = Field(min_length=1, max_length=500)
    quantity: float = Field(gt=0)
    unit_price_cents: int = Field(ge=0)     # INTEGER for money
    total_cents: int = Field(ge=0)

    @field_validator("total_cents")
    def total_matches(cls, v, info):
        expected = round(info.data["quantity"] * info.data["unit_price_cents"])
        if abs(v - expected) > 1: raise ValueError(...)
        return v
```

Rules that earn their keep:
- **Integers for money, never floats** (cents/pence/bps). Floats hallucinate trailing decimals; precision loss compounds.
- **`additionalProperties: false`** (Pydantic `extra="forbid"`). Reject unknown fields at the schema layer.
- **Tight `Literal[...]` enums** instead of `str`. `currency: Literal["USD","EUR","GBP"]`.
- **Cross-field validators** (line total = qty × price). Catches arithmetic hallucinations.
- **`min_length=1` on required arrays.** Empty list usually means the extractor gave up silently.
- **Explicit `Optional[X] = None`** for absent fields; the model needs to know `null` is acceptable.
- **Regex patterns + bounded numerics** for IDs and amounts; defends against hallucinated "999999999" values.

Anti-rule: don't use `Any` or unconstrained `dict`. Validates anything; protects nothing.

## 2. Preprocess — reduce noise

| Input | Preprocess |
|---|---|
| PDF (text) | `pdfplumber` / `pypdfium2` |
| PDF (scanned) | Vision model directly OR OCR → text |
| HTML | `trafilatura` / `readability-lxml` |
| Email | `mailparser`; preserve headers, strip quoted replies |
| Transcript | Speaker tags + timestamps preserved |

Detect-and-route by input type beats one monolithic prompt. Don't blindly truncate long inputs — chunk by logical unit (page, message, paragraph), extract per-chunk, merge. Blind truncation drops fields silently.

## 3. Prompt structure — three pillars, in order

1. **Schema injection** — pass JSON Schema via `response_format` (OpenAI `strict: true`), `tools.input_schema` (Anthropic), or `response_schema` (Gemini). Server-side enforcement eliminates the most common failure (malformed JSON) before your code runs.
2. **Few-shot examples** — 2-5 demonstrations covering the easy case, an edge case, a "missing field returns null" case. 0-shot accuracy on non-trivial extractions is 20-40% below 3-shot.
3. **Instructions** — small, specific, imperative: "Return ONLY JSON. Money in smallest unit (cents). Dates ISO 8601. Unknown fields → null (do NOT invent)."

## 4. Validate-correct loop

```python
MAX_CORRECTIONS = 2
for attempt in range(MAX_CORRECTIONS + 1):
    response = await llm.complete(messages=messages, response_format=SCHEMA)
    try:
        return Invoice.model_validate_json(response.content)
    except ValidationError as e:
        if attempt == MAX_CORRECTIONS:
            raise ExtractionFailed(last_error=e, last_response=response.content)
        messages.extend([
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": f"Validation failed: {e}. Return only the corrected JSON."},
        ])
```

Empirically (frontier models, well-defined schemas): 1 correction round resolves >90%, 2 rounds reach >99%. If you regularly hit 3 rounds, the schema or prompt is wrong, not the model. Track correction-loop rate as a metric — sustained >10% signals drift.

## 5. Fallback strategies

- **Model ladder**: Haiku → Sonnet → Opus. Start cheapest; escalate only what fails. If Haiku succeeds 90%, you save substantial cost.
- **Per-field fallback**: when document-level extraction fails, ask separately for the problematic field with a focused prompt.
- **Vision fallback for PDFs**: low char-count after text extraction → route through vision model.
- **Human review queue**: items that fail all automated paths surface partial extraction + raw input to an operator. Corrected outputs become future few-shot examples.

Don't dev/null failures — they're your best learning signal.

## 6. Observability

Per extraction: `{extraction_id, input_type, model, tokens_in, tokens_out, cost_usd, validation_corrections, fallback_used, outcome, latency_ms}`.

Aggregate metrics:
- **Success rate per `input_type`** — alert below threshold.
- **Per-field fill rate** — `vendor_tax_id` normally 87%, dropping to 40% = input or prompt change.
- **Correction-loop rate** — rising = drift.
- **Cost per extraction** — for budget alerts.

Per-field fill-rate tracking is uniquely powerful for continuously-running extraction; equivalent of integration-test coverage.

## 7. Cost / latency budget — model BEFORE shipping

```
Per extraction (Sonnet, 5K in + 0.5K out, ×1.2 correction multiplier): ~$0.027
Volume: 10K/day → Monthly: ~$8,100

Optimisations:
- Model ladder (Haiku-first for easy 70%): ~$3K/mo savings
- Prompt caching (stable system prompt): ~30% on input
- Batch API (24h latency tolerable): ~50% on input+output
```

Use average tokens (not minimum); apply correction multiplier; project at 10× volume. See `[[relatedTo::LLM Workflow Cost Modelling]]`.

## Anti-patterns

- `additionalProperties: true` schema (model invents fields).
- `Any` or unconstrained `str` types.
- No few-shot examples (20-40% accuracy hit).
- Trusting first response (Sonnet ~5% wrong on moderate schemas).
- Blind truncation of long input (silent field loss).
- No raw-response logging (can't debug failures).
- No human-review queue (failures vanish; pipeline never improves).
- One mega-prompt for many input types.

## Links

- [[relatedTo::Function Calling Reliability Patterns]]
- [[relatedTo::LLM Workflow Cost Modelling]]
- [[relatedTo::Prompt Engineering Patterns - 2026 Research]]
- [[relatedTo::Retry Policy Design for Distributed Operations]]
- [[buildsOn::Pydantic v2]]

## References

- OpenAI Structured Outputs: https://platform.openai.com/docs/guides/structured-outputs
- Anthropic Tool Use: https://docs.anthropic.com/en/docs/build-with-claude/tool-use
- Pydantic v2 validators: https://docs.pydantic.dev/latest/concepts/validators/
