---
title: Structured Output
type: concept
tags: [AI, LLM, JSON, schema, output-parsing, tool-calling, type-safety]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:59Z
status: active
---

## Overview

Structured Output refers to techniques that constrain LLM generation to produce outputs conforming to a specific schema (JSON, XML, typed objects) rather than free-form text. This is critical for programmatic use of LLM outputs in downstream pipelines, tool calling, and data extraction tasks.

## Methods

### 1. JSON Mode (API-Level)

Most modern LLM APIs support a JSON mode that guarantees valid JSON output:
```python
# OpenAI / compatible APIs
response = client.chat.completions.create(
    model="gpt-4o-mini",
    response_format={"type": "json_object"},
    messages=[{"role": "user", "content": "Extract entities: ..."}]
)
# Guaranteed valid JSON, but schema not enforced
```

### 2. Structured Outputs with Schema (OpenAI, Anthropic)

Newest APIs support schema-constrained generation:
```python
from pydantic import BaseModel

class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

response = client.beta.chat.completions.parse(
    model="gpt-4o",
    messages=[...],
    response_format=CalendarEvent,
)
event = response.choices[0].message.parsed  # CalendarEvent object
```

### 3. Guided Decoding

Applied at inference time to constrain token sampling to valid schema tokens:
- **Outlines** (Python library) — grammar-based structured generation
- **LMQL** — LLM query language with constraints
- **SGLang** — structured generation language
- **vLLM Guided Decoding** — JSON Schema or regex constraints

```python
import outlines

model = outlines.models.transformers("mistral-7b")

@outlines.prompt
def extraction_prompt(text):
    """Extract the entity from: {{ text }}"""

generator = outlines.generate.json(model, schema)
result = generator(extraction_prompt("Apple Inc was founded in 1976"))
# Returns: {"company": "Apple Inc", "founded": 1976}
```

### 4. Prompt-Based JSON Extraction

Without API support, instruct the model to produce JSON:
```python
prompt = """Extract entities from the following text and return JSON:

Text: {text}

Return ONLY valid JSON with this structure:
{{
  "entities": [
    {{"name": "...", "type": "...", "value": "..."}}
  ]
}}

JSON:"""
```

**Reliability tips**:
- End prompt with `JSON:` to prime JSON generation
- Specify schema explicitly in the prompt
- Add validation and retry on parse failure
- Use few-shot examples

### 5. Function Calling / Tool Calling

Modern LLMs support tool calling natively — structured output as tool parameters:
```python
tools = [{
    "name": "extract_entities",
    "description": "Extract named entities from text",
    "parameters": {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {"type": "string"}
            }
        },
        "required": ["entities"]
    }
}]
```

## Parsing and Validation

```python
import json
from pydantic import BaseModel, ValidationError

def parse_llm_json(text: str, schema: BaseModel) -> BaseModel:
    # Extract JSON from potentially messy LLM output
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if not json_match:
        raise ValueError("No JSON found in LLM output")

    try:
        data = json.loads(json_match.group())
        return schema.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"JSON validation failed: {e}")
```

## Reliability Hierarchy

From most to least reliable:
1. **Native structured output API** (schema-constrained at API level)
2. **Guided decoding** (constrained at token level)
3. **Tool calling** (function signature constrains format)
4. **JSON mode** (valid JSON but schema not enforced)
5. **Prompt-based** (pure text prompt; requires parsing + retry)

## Common Failure Modes

- **Truncated JSON**: context window exceeded before closing brackets
- **Schema mismatch**: model generates extra/missing fields
- **Type coercion**: "123" instead of 123 (string vs. integer)
- **Nested escaping**: JSON within JSON strings causes parse errors

## Related Links

[[relatedTo::Guided Decoding with JSON Schema]]
[[relatedTo::Fine-Tuning for Tool Calling]]
[[relatedTo::Model Inference Formats]]
[[relatedTo::Agentic LLM Workflows]]
[[relatedTo::VLM Table Extraction Strategies]]
