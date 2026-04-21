# Prompt Patterns Examples

## Pattern 1: Few-Shot Learning

### Good Example
```markdown
Classify sentiment as positive, negative, or neutral.

Examples:
Input: "This product is amazing! Best purchase ever." → positive
Input: "Terrible quality, very disappointed." → negative
Input: "It works as expected, nothing special." → neutral
Input: "Arrived on time, decent packaging." → neutral
Input: "Love it! Exceeded my expectations." → positive

Now classify:
Input: "Pretty good, some minor issues but overall satisfied." →
```

**Why It Works**:
- 5 examples (optimal for most tasks)
- Balanced representation (2 positive, 1 negative, 2 neutral)
- Clear format (Input → Output)
- Covers edge cases (neutral examples important)

---

### Bad Example
```markdown
Classify the sentiment.

Example: "Great product" is positive.

Input: "Decent quality" →
```

**Why It Fails**:
- Only 1 example (too few)
- No negative/neutral examples
- Vague instruction ("classify the sentiment" - into what?)
- Format inconsistent

---

## Pattern 2: Chain-of-Thought

### Good Example
```markdown
Solve this math problem step-by-step:

A store offers 15% discount on a $240 item. After discount, there's 8% sales tax. What's the final price?

Think through it:
1. Calculate discount amount
2. Subtract discount from original price
3. Calculate sales tax on discounted price
4. Add tax to discounted price

Show your work:
```

**Why It Works**:
- Explicit "step-by-step" instruction
- Breaks down reasoning process
- "Show your work" encourages transparency
- Structured steps guide LLM

---

### Bad Example
```markdown
What is 15% off $240 with 8% tax?

Answer:
```

**Why It Fails**:
- No step-by-step guidance
- LLM might skip steps
- Harder to verify reasoning
- More prone to calculation errors

---

## Pattern 3: Output Format Specification

### Good Example
```markdown
Summarize this article in the following JSON format:

{
  "main_topic": "One sentence describing the main topic",
  "key_points": [
    "First key point",
    "Second key point",
    "Third key point"
  ],
  "conclusion": "One sentence summary of conclusion",
  "word_count": <number of words in original>
}

Article: [article text]

JSON Output:
```

**Why It Works**:
- Exact format specified (JSON)
- Example structure provided
- Field descriptions clear
- Parseable output guaranteed

---

### Bad Example
```markdown
Summarize this article with main topic, key points, and conclusion.

Article: [text]
```

**Why It Fails**:
- Format ambiguous (prose? bullets? JSON?)
- No example
- Inconsistent output structure
- Hard to parse programmatically

---

## Pattern 4: Constraint Enforcement

### Good Example
```markdown
Write a product description with these constraints:

REQUIREMENTS:
- Length: Exactly 3 sentences (no more, no less)
- Tone: Professional, informative (not salesy or hyperbolic)
- Include: Features, benefits, target audience
- DO NOT use: Superlatives ("best", "amazing", "perfect", "revolutionary")
- DO NOT use: Exclamation marks
- Focus: Factual, specific, measurable claims

Product: [details]

Description:
```

**Why It Works**:
- Explicit boundaries (length, tone, content)
- Positive constraints (what to include)
- Negative constraints (what to avoid)
- Clear reasoning (why these constraints matter)

---

### Bad Example
```markdown
Write a good product description. Keep it short and professional.

Product: [details]
```

**Why It Fails**:
- Vague ("short" = how many sentences?)
- No constraints on tone (what's "professional"?)
- No examples of good vs bad
- Easy for LLM to ignore

---

## Pattern 5: Role-Based Prompting

### Good Example
```markdown
You are a senior Python developer with 10+ years experience, specializing in security and best practices.

Review this code for security vulnerabilities:
- SQL injection risks
- Input validation gaps
- Sensitive data exposure (passwords, API keys)
- Authentication/authorization issues
- Error handling that leaks information

For each issue found:
1. Severity: Critical/High/Medium/Low
2. Location: Line number or function name
3. Issue: What's wrong
4. Fix: Specific code change

Code:
[code]

Security Review:
```

**Why It Works**:
- Specific role ("senior Python developer", not just "developer")
- Domain expertise ("specializing in security")
- Clear scope (what to review)
- Structured output format
- Examples of what to look for

---

### Bad Example
```markdown
Review this code for issues.

Code: [code]
```

**Why It Fails**:
- No role assignment (generic "you")
- "Issues" too vague (bugs? style? security? performance?)
- No output format
- No expertise level set

---

## Pattern 6: Context + Constraint + Example

### Good Example (Combining Patterns)
```markdown
You are an API documentation writer.

Task: Generate OpenAPI 3.0 spec for this endpoint.

Constraints:
- Include: description, parameters, request body, responses
- Parameter types: Specify type, required/optional, format
- Response codes: 200, 400, 401, 404, 500
- Examples: Provide request and response examples

Format: Valid YAML (OpenAPI 3.0)

Endpoint Details:
GET /users/{userId}
- Fetches user by ID
- Requires authentication (Bearer token)
- Returns user object or 404 if not found

OpenAPI Spec:
```yaml
# Your output here
```

**Why It Works**:
- Role (API doc writer)
- Task (generate OpenAPI spec)
- Constraints (what to include, response codes)
- Format (YAML, specific version)
- Context (endpoint details)
- Example structure (YAML code block)

---

## Pattern 7: Hallucination Prevention

### Good Example
```markdown
Answer the question based ONLY on the context below. If the answer is not in the context, respond with: "I don't know based on the provided information."

DO NOT make assumptions or use external knowledge.
DO NOT invent facts not present in the context.
If partially unsure, state what you know and what's unclear.

Context:
[provided context]

Question: [user question]

Answer:
```

**Why It Works**:
- Explicit "ONLY" constraint
- Fallback phrase provided
- Multiple warnings against hallucination
- Encourages partial answers when appropriate

---

### Bad Example
```markdown
Answer this question based on the context.

Context: [text]
Question: [question]
```

**Why It Fails**:
- No fallback for missing info
- Doesn't forbid external knowledge
- LLM might fill gaps with hallucinations

---

## Pattern 8: Iterative Refinement

### Good Example
```markdown
Task: Generate a SQL query for this requirement.

Step 1: Understand the requirement
[requirement]

Step 2: Identify tables and columns needed
- Tables: [list tables]
- Columns: [list columns]

Step 3: Plan the query structure
- JOINs needed: [describe]
- Filters: [WHERE clauses]
- Aggregations: [GROUP BY, HAVING]

Step 4: Write the SQL query
```sql
[query]
```

Step 5: Explain the query
[explanation of what the query does]

Execute this step-by-step.
```

**Why It Works**:
- Breaks complex task into steps
- Forces LLM to show reasoning
- Each step validates the next
- Final output is well-reasoned

---

## Pattern 9: Multi-Turn Prompting

### Good Example (First Turn)
```markdown
I need to design a REST API for a blog platform.

First, help me identify the main resources and their relationships. Don't design endpoints yet, just list:
1. Resources (e.g., User, Post, Comment)
2. Relationships between resources
3. Key attributes for each resource

We'll design the actual endpoints in the next step.
```

### Follow-up (Second Turn)
```markdown
Based on those resources, now design the RESTful endpoints:
- Use standard HTTP methods (GET, POST, PUT, DELETE)
- Follow REST naming conventions
- Include pagination for list endpoints
- Specify response codes

Format as a table:
| Method | Endpoint | Description | Response Code |
```

**Why It Works**:
- Breaks complex task (API design) into stages
- First turn: High-level thinking
- Second turn: Detailed design
- Each turn builds on previous

---

## Pattern 10: Validation + Correction

### Good Example
```markdown
Generate 5 unit tests for this function.

After generating, review each test for:
1. Does it test a distinct scenario?
2. Are assertions meaningful?
3. Is test data realistic?

If any test fails review, improve it.

Function:
[code]

Tests:
[tests]

Self-Review:
[review of each test]

Final Tests (after improvements):
[corrected tests]
```

**Why It Works**:
- Generate → Review → Improve cycle
- Specific review criteria
- Self-correction step
- Higher quality output

---

## Quick Reference Card

| Pattern | When to Use | Key Element |
|---------|-------------|-------------|
| Few-Shot | Classification, extraction, formatting | 2-5 examples |
| Chain-of-Thought | Math, logic, multi-step reasoning | "Think step-by-step" |
| Output Format | Need structured/parseable output | Explicit format + example |
| Constraint | Control tone, length, content | "DO/DO NOT" lists |
| Role-Based | Domain expertise needed | "You are a [expert]" |
| Context-Only | Prevent hallucinations | "ONLY from context" |
| Iterative | Complex multi-part tasks | Break into steps |
| Multi-Turn | Very complex workflows | Separate turns for stages |
| Validation | High-quality output needed | Generate → Review → Improve |
