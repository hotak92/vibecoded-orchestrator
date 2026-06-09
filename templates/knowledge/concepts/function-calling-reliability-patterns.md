---
title: Function Calling Reliability Patterns
type: pattern
tags:
  - AI
  - LLM
  - function-calling
  - structured-output
  - reliability
  - automation
  - mid-level-architecture
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
status: active
---

# Function Calling Reliability Patterns

LLM function calling (Anthropic "tool use", OpenAI "tools", Gemini "function calling") looks deterministic in demos and is not in production. These patterns make it production-grade.

## The four failure modes

Every reliability pattern below targets one of these:

1. **Malformed JSON** — token-level corruption, truncation, hallucinated structure.
2. **Schema violation** — valid JSON but missing required fields, wrong types, extra fields, out-of-range values.
3. **Wrong tool** — model calls `cancel_order` when it should have called `refund_order`; semantic mistake.
4. **Loop / no-call** — model keeps calling tools that don't progress, or refuses to call any tool when it should.

## Pattern 1: Strict JSON Schema (catches modes 1 + 2)

Every function definition gets a JSON Schema with `"additionalProperties": false`, required fields, and concrete types. Don't accept `Any` or `dict`.

```python
schema = {
    "name": "create_invoice",
    "description": "Create a new invoice for a customer. Use when the user explicitly asks to bill someone.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["customer_id", "amount_cents", "currency"],
        "properties": {
            "customer_id": {"type": "string", "pattern": "^cus_[A-Za-z0-9]+$"},
            "amount_cents": {"type": "integer", "minimum": 1, "maximum": 10_000_000},
            "currency": {"type": "string", "enum": ["USD", "EUR", "GBP"]},
            "note": {"type": "string", "maxLength": 500},
        },
    },
}
```

Anthropic's API enforces input_schema server-side for tool use — invalid calls don't reach your code. OpenAI's `strict: true` mode (2024+) does the same. Use both when available.

## Pattern 2: Validate-correct loop (catches mode 2 even when the model providers don't)

When server-side enforcement is unavailable or you have semantic constraints the schema can't express:

```python
import jsonschema
from pydantic import BaseModel, ValidationError

MAX_CORRECTIONS = 3

def call_with_validation(prompt: str, schema: dict, validator):
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(MAX_CORRECTIONS):
        response = llm.complete(messages=messages, response_format={"type": "json_object"})
        try:
            parsed = json.loads(response.content)
            validator(parsed)  # raises on schema/semantic failure
            return parsed
        except (json.JSONDecodeError, jsonschema.ValidationError, ValidationError) as e:
            # Feed the error back to the model
            messages.extend([
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": f"Your previous response failed validation: {e}. Return only valid JSON matching the schema."}
            ])
    raise FunctionCallingFailure(f"Model failed validation after {MAX_CORRECTIONS} corrections")
```

Empirically, a single correction round resolves >90% of failures from frontier models on well-defined schemas; two rounds reach >99%. If you're consistently hitting three rounds, the schema or prompt is the problem, not the model.

## Pattern 3: Tool description discipline (catches mode 3)

Tool selection accuracy is overwhelmingly driven by the `description` field:

- **State the trigger explicitly**: "Use when the user asks to cancel a subscription. Do NOT use for one-time charges or pauses."
- **Disambiguate vs. siblings**: "Unlike `refund_order` which returns money, this tool only marks the order as cancelled without financial action."
- **Document side effects**: "This is irreversible and sends a cancellation email."
- **List preconditions**: "Requires the subscription to be in `active` or `past_due` state; will fail otherwise."

Tool names matter too. `update_thing` is worse than `update_customer_billing_address`. Disambiguate by name and description, not by hoping the model infers from context.

## Pattern 4: Pre-call guards (defence in depth)

Even with a perfect tool call, validate again on your side:

```python
def cancel_subscription(subscription_id: str):
    # Defence: business invariants the schema can't express
    sub = db.get_subscription(subscription_id)
    if not sub:
        return {"error": "subscription not found", "subscription_id": subscription_id}
    if sub.status == "cancelled":
        return {"error": "already cancelled", "cancelled_at": sub.cancelled_at}
    # Defence: authorization
    if not current_actor.can_cancel(sub):
        return {"error": "unauthorized"}
    # Now actually do the thing
    ...
```

Returning structured errors lets the model recover gracefully (see Pattern 6).

## Pattern 5: Tool-call budgets (catches mode 4)

Cap the agent's run with multiple budgets:

```python
config = AgentConfig(
    max_tool_calls=20,
    max_tokens=50_000,
    max_wall_clock_seconds=120,
    max_consecutive_same_tool=3,  # detect loops
)
```

When a budget trips, return a graceful failure to the caller with the partial trace, not a crash. Loops most commonly arise from:
- The model calling a search tool and not making progress on the results.
- A tool that returns inconclusive output ("rate limited, try again later") that the model keeps re-trying.
- The model lacks a "stop" tool to declare it's done. Provide one (`finish_task(summary: str)`).

## Pattern 6: Error-as-data (rescues mode 4 instead of failing)

When a tool fails, return a structured error AS the tool result instead of throwing. The model will adapt.

```json
// Good: model can adapt
{"error": "rate_limited", "retry_after_seconds": 30, "suggestion": "Wait or use the cached_search tool"}

// Bad: kills the agent
Exception("RateLimitError")
```

This is the single highest-leverage reliability pattern. Most production agents are 80% adversarial-tool-design: anticipate every failure your tool can throw and turn it into actionable feedback for the model.

## Pattern 7: Eval harness (catches everything, regresssion-style)

You can't improve what you don't measure. Build an eval set:

```python
# tests/agent_evals/test_invoice_creation.py
@pytest.mark.parametrize("scenario", [
    {"prompt": "bill Alice $50", "expects_tool": "create_invoice", "expects_amount": 5000},
    {"prompt": "send Alice an invoice for fifty bucks", "expects_amount": 5000},
    {"prompt": "charge Alice nothing", "expects_tool": None, "expects_clarification": True},
])
def test_invoice_scenarios(scenario):
    result = agent.run(scenario["prompt"])
    assert_tool_called(result, scenario.get("expects_tool"))
    ...
```

Run on every prompt/model/schema change. `promptfoo`, `Inspect AI`, and DIY pytest all work. ~30 scenarios covers most regressions; 100+ for safety-critical agents.

## Pattern 8: Observability hooks

Per turn, emit:
- `model`, `model_version`, `tokens_in`, `tokens_out`, `cost_usd`
- `tool_calls_attempted`, `tool_calls_validated`, `tool_calls_failed`
- `validation_corrections` (Pattern 2 round-trip count)
- `outcome` (`success`, `budget_exhausted`, `validation_failed`, `tool_error`)

OTel traces with span-per-tool-call let you compute aggregate metrics (p50/p99 latency per tool, tool error rates, model drift over time).

## Anti-patterns

- **Relying on the model to do retries** — it'll either give up or loop forever. Retry at YOUR layer with explicit budgets.
- **`Any` types in the schema** — defeats validation; the model will sometimes output a string, sometimes an object.
- **Tool returning a giant blob** — model context fills up, errors propagate. Truncate / summarise tool outputs.
- **One mega-tool with 20 parameters** — split into 3-5 focused tools; tool selection accuracy drops sharply past ~10 parameters.
- **Trusting the model's output is "probably fine"** — every tool call hits real downstream systems; validate every time.

## Links

- [[relatedTo::Agent Framework Comparison 2026]]
- [[relatedTo::Structured Output Extraction Pattern]]
- [[buildsOn::Prompt Engineering Patterns - 2026 Research]]
