---
name: structured-output-extraction
description: Builds a reliable LLM-powered extraction pipeline for messy inputs (PDFs, emails, transcripts, HTML) into a strict JSON schema with validation, automated correction loop, and observability. Use when designing extraction from unstructured documents or hardening one that fails too often
short_desc: LLM JSON extraction pipeline with validation + corrections
keywords: [JSON extraction, schema validation, extraction pipeline, LLM extraction, Pydantic, unstructured data, "extract from PDF", "extract from email", "structured output", "JSON schema", "messy input", "extraction from documents"]
model: opus
effort: high
---

# Structured Output Extraction (Opus)

**Purpose**: Design a pipeline that turns messy unstructured input (PDFs, emails, transcripts, HTML, customer-support tickets) into validated structured data matching a strict schema. Covers schema design, prompt structure, validate-correct loop, fallback strategies, observability, and cost/latency budgeting.

**Model**: Opus 4.7

## When to invoke autonomously

Invoke when:
1. **New extraction task**: "Pull line items from invoice PDFs", "Extract action items from meeting transcripts", "Parse resumes into structured candidate records".
2. **Hardening an existing pipeline**: "Our extraction breaks on 20% of inputs — fix it."
3. **Schema design**: "What's the right JSON schema for this extraction task?"
4. **Cost reduction**: "Extraction costs are blowing the budget — how do we shrink them?"

**Don't invoke for**:
- Structured-to-structured transformation (just code it).
- Strict OCR-only tasks (use a vision model directly, no schema design needed).
- Agentic tool-use workflows (use `@ai-agentic-architect` or the function-calling reliability KG node).

## Usage

```
/structured-output-extraction design for [task] with [input type]
/structured-output-extraction audit [path to existing extractor]
/structured-output-extraction harden [failure mode]
```

## The pipeline

```
Input → Preprocess → Prompt → LLM → Parse → Validate → [Correct]* → Persist
                                                    │
                                                    └── fail → DLQ + human review
```

### 1. Schema design (the most important step)

Strict JSON Schema with `additionalProperties: false`. Every field gets a type, a constraint, and a description.

```python
from pydantic import BaseModel, Field, field_validator
from typing import Literal
from datetime import date

class InvoiceLineItem(BaseModel):
    model_config = {"extra": "forbid"}
    description: str = Field(min_length=1, max_length=500)
    quantity: float = Field(gt=0)
    unit_price_cents: int = Field(ge=0, description="Per-unit price in cents")
    total_cents: int = Field(ge=0)
    tax_rate_bps: int = Field(ge=0, le=10_000, description="Tax rate in basis points (0.01%); e.g. 1000 = 10%")

    @field_validator("total_cents")
    @classmethod
    def total_matches(cls, v, info):
        expected = round(info.data["quantity"] * info.data["unit_price_cents"])
        if abs(v - expected) > 1:  # allow 1-cent rounding
            raise ValueError(f"total {v} does not match qty*price = {expected}")
        return v

class Invoice(BaseModel):
    model_config = {"extra": "forbid"}
    invoice_number: str = Field(pattern=r"^[A-Z0-9-]{3,32}$")
    issue_date: date
    due_date: date
    currency: Literal["USD", "EUR", "GBP", "AUD"]
    vendor_name: str = Field(min_length=1, max_length=200)
    vendor_tax_id: str | None = None
    line_items: list[InvoiceLineItem] = Field(min_length=1, max_length=1000)
    total_cents: int = Field(ge=0)
    notes: str | None = Field(default=None, max_length=2000)
```

**Schema design rules**:
- Use integers for money (cents/pence), never floats. Floats lose precision and the model often hallucinates trailing decimals.
- Constrain enums tightly (`Literal["USD", "EUR", "GBP", "AUD"]` not `str`).
- Add cross-field validators (line totals = qty × price; sum of lines = invoice total).
- Use `min_length=1` on required arrays — empty list often means the extractor gave up silently.
- Keep optional fields explicitly `Optional` with `default=None`. The model needs to know `null` is acceptable.

### 2. Preprocess

Reduce noise before the LLM sees it:

| Input | Preprocess |
|---|---|
| PDF | Extract text with `pdfplumber` (text-based) or `pypdfium2`; vision model fallback if scanned |
| HTML | `trafilatura.extract()` or `readability-lxml` to strip nav/ads |
| Email | Parse via `mailparser`; preserve subject + sender; strip quoted replies if not needed |
| Transcript | Speaker diarization, timestamp formatting, light cleanup |
| Image-only | Vision model with high resolution, ask for structured output directly |

**Truncation**: if input > model's effective context, chunk by logical unit (page, message, paragraph) and extract per-chunk, then merge. Don't blindly truncate to fit — you'll drop fields that appear in the cut-off part.

### 3. Prompt structure

Three pillars, applied in this order:

1. **Schema injection**: pass the JSON Schema explicitly (most providers support this via `response_format` / `tools`).
2. **Few-shot examples**: 2-5 demonstrations of input → expected output. Cover the easy case, an edge case, a "missing field" case.
3. **Instructions**:
   - "Return ONLY JSON matching the schema. No commentary."
   - "If a field is unknown, use `null` (do not invent).
   - "Money values are in the smallest currency unit (cents). USD$1.50 → 150."
   - "Dates are ISO 8601 (YYYY-MM-DD)."

```python
SYSTEM = """You extract structured data from invoices.

CRITICAL RULES:
- Return ONLY a JSON object matching the schema. No prose, no markdown.
- Money values are integers in the smallest unit (cents for USD).
- Dates are ISO 8601 strings (YYYY-MM-DD).
- If a field is unknown or absent in the input, use null. Do NOT invent.
- Line item totals MUST equal quantity × unit price (within 1 cent rounding).
- If the input does not appear to be an invoice, return {"error": "not_an_invoice"}.
"""

USER = f"""Extract the invoice into JSON matching this schema:

{json.dumps(Invoice.model_json_schema(), indent=2)}

Examples:
{EXAMPLES}

Invoice text:
---
{invoice_text}
---
"""
```

### 4. Validate-correct loop

```python
MAX_CORRECTIONS = 2

async def extract(text: str) -> Invoice:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_user_prompt(text)}]
    for attempt in range(MAX_CORRECTIONS + 1):
        response = await llm.complete(
            messages=messages,
            response_format={"type": "json_schema", "json_schema": Invoice.model_json_schema()},
            max_tokens=4000,
        )
        try:
            return Invoice.model_validate_json(response.content)
        except ValidationError as e:
            if attempt == MAX_CORRECTIONS:
                metrics.extraction_failure.inc()
                raise ExtractionFailed(text=text, last_error=e, last_response=response.content)
            metrics.extraction_correction.inc()
            messages.extend([
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": f"Your output failed validation: {e}. Fix the JSON and return ONLY the corrected object."},
            ])
```

See `knowledge/concepts/function-calling-reliability-patterns.md` for the full pattern.

### 5. Fallback strategies

When the LLM consistently fails on certain inputs:

- **Per-field fallback**: ask separately for the problematic field with a focused prompt.
- **Model ladder**: try Haiku → on failure escalate to Sonnet → on failure escalate to Opus → on failure DLQ.
- **Vision fallback for PDFs**: if text extraction yields a near-empty document, the PDF is image-based; route through a vision model.
- **Human review queue**: items that fail all automated paths go to a queue with the partial extraction surfaced for an operator.

### 6. Observability

Per extraction:

```json
{
  "extraction_id": "ext_abc123",
  "input_type": "invoice_pdf",
  "input_chars": 4271,
  "preprocessing_ms": 84,
  "model": "claude-sonnet-4-6",
  "tokens_in": 5012,
  "tokens_out": 487,
  "cost_usd": 0.018,
  "validation_corrections": 1,
  "outcome": "success",
  "latency_ms": 3142
}
```

Aggregate metrics:
- Success rate per input_type (alert if drops below 95%)
- p50 / p99 latency
- Cost per extraction (budget alert)
- Correction-loop rate (rising = schema or prompt drift)
- Per-field fill rate (if `vendor_tax_id` fill rate drops, your prompt or your inputs changed)

### 7. Cost + latency budget

Sample math for the design:

```
Invoice extraction:
- Input tokens: avg 5K (PDF text)
- Output tokens: avg 500
- Model: Opus 4.7 ($3/M input, $15/M output)
- Cost per extraction: 5K × $3/M + 500 × $15/M = $0.015 + $0.0075 = $0.0225
- Volume: 10K/day
- Daily cost: $225
- Monthly cost: $6,750

If unacceptable, options:
1. Haiku for easy cases + Sonnet for hard cases (model router) → ~40% reduction.
2. Smaller schema (drop optional fields) → ~10% reduction.
3. Caching (same invoice hashed → reuse extraction) → highly variable.
4. Batch API where supported → 50% reduction at the cost of latency.
```

Always include these numbers in the design doc.

## Worked examples

### Easy: extracting action items from meeting transcripts

- Schema: `list[ActionItem]` where each has `assignee_name`, `action`, `due_date_iso | null`, `confidence: float`.
- Preprocess: light cleanup, speaker tags preserved.
- Few-shot: 3 examples (clear action, implicit action, no actions found).
- Validation: assignee must be a string; due_date must be valid ISO or null.

### Medium: extracting line items from invoices

- Schema as above.
- Preprocess: `pdfplumber` text first, vision fallback if char count < threshold.
- Few-shot: 3 examples covering simple, multi-currency, missing tax_id.
- Cross-field validation enforced; corrections allowed.

### Hard: parsing legal contracts

- Schema with deeply nested clause structure.
- Preprocess: section detection, normalize defined-term references.
- Few-shot: NOT enough — fine-tune on labelled examples or use longer context with chunking.
- Validation: legal-domain checks (e.g. governing-law jurisdiction is a known value).
- Almost certainly needs human review for >X% of cases; design the human-loop queue.

## Anti-patterns

- **Schema with `additionalProperties: true`** — model fills in fields you didn't ask for, downstream consumers break.
- **Using `Any` or unconstrained `str`** — invites hallucination.
- **No examples in the prompt** — accuracy drops 20-40% without few-shot for non-trivial extractions.
- **Trusting the first response** — even Sonnet gets ~5% wrong on first try for moderately complex schemas; validate.
- **No cost monitoring** — extraction silently becomes the biggest line item.
- **Truncating input blindly** — drops fields, returns valid-looking but wrong output.
- **Not persisting the raw response** — when extraction is wrong, you can't debug without the original LLM output.

## Output format

```markdown
# Extraction Design: {task}

## Input
- Format: {PDF | HTML | email | transcript | ...}
- Avg size: {tokens / chars}
- Volume: {N per day, peak burst}

## Target schema
{Pydantic / JSON Schema}

## Preprocessing
{steps}

## Prompt structure
- System: {summary}
- Few-shot: {N examples, edge cases covered}
- Schema injection: {response_format strategy}

## Validation
- Schema validation: pydantic / json-schema
- Cross-field rules: {list}
- Correction loop: max {N} rounds

## Fallback
- {model ladder | vision fallback | human queue}

## Observability
- Metrics: {list}
- Alerts: {thresholds}

## Cost envelope
- Per extraction: ${X}
- Daily: ${Y}
- Monthly: ${Z}
- Optimisations available: {list with savings estimates}

## Tests
- Happy path: clean input → correct extraction
- Edge cases: missing fields, multi-line tables, OCR-noise
- Adversarial: prompt-injection attempts, oversized inputs, malformed
- Regression: 30+ labelled examples, asserted on every prompt/model change
```

## Knowledge graph integration

Source: `knowledge/concepts/function-calling-reliability-patterns.md` (validate-correct pattern), `knowledge/patterns/prompt-engineering-2026.md` (few-shot, role).

After designing, write `knowledge/projects/extraction-{task}.md` capturing the chosen schema, prompt structure, observed accuracy, and per-field fill rates.

## Knowledge Systems

> **Full reference**: [`~/.claude/shared/KNOWLEDGE_SYSTEMS.md`](~/.claude/shared/KNOWLEDGE_SYSTEMS.md)

**Decision tree**:
- Known terms → `kg-search` CLI
- Conceptual → `hybrid_search` MCP
- Relationships → `semantic_graph_search` MCP
- Code by purpose → `search_code_graph` MCP
- Literal strings → Grep

## Success metrics

- Schema is strict (`additionalProperties: false`, typed enums, integer money).
- Validate-correct loop with bounded retries; metrics on correction rate.
- Cost per extraction within budget at expected volume.
- Per-field fill rate tracked; alert on regression.
- Failure path goes to a human-review queue, not silent dev/null.
- Test suite includes labelled golden examples that gate prompt/model changes.
