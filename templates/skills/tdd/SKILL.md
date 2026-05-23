---
name: tdd
description: Test-Driven Development workflow. Write failing test first, then implement to make it pass. Use for new features, bug fixes, or refactoring with confidence.
keywords: [TDD, "test-driven", "red-green-refactor", "failing test first", "write the test first"]
argument-hint: "[feature-or-bug-description]"
model: sonnet
---

# /tdd [feature or function description]

Strict Red → Green → Refactor TDD cycle.

## Usage

```
/tdd validate_user_email function      # TDD a specific function
/tdd rate limiting middleware          # TDD a new middleware
/tdd                                   # TDD current task from CONTEXT_STATE.md
```

## The Cycle

### 🔴 RED — Write a Failing Test

1. Read the specification (task description, docstring, or requirements)
2. Identify the smallest testable behavior
3. Write ONE test that captures that behavior:
   - Test name describes behavior: `test_<function>_<scenario>_<expected_outcome>`
   - Assert on observable outputs, not implementation details
   - Use pytest fixtures for setup

4. Run test → confirm it **fails** for the right reason:
   ```bash
   pytest tests/ -k "your_test_name" -v
   ```
   - "Import error" or "AttributeError" = file/function doesn't exist yet → OK
   - "AssertionError" = function exists but wrong behavior → OK
   - Test passes without any implementation → test is wrong, rewrite it

### 🟢 GREEN — Make It Pass

5. Write the **minimum code** to make the test pass
   - No extra features, no future-proofing
   - Ugly code is fine at this stage
   - Don't optimize yet

6. Run test → confirm it **passes**:
   ```bash
   pytest tests/ -k "your_test_name" -v
   ```

7. Run full suite → confirm no regressions:
   ```bash
   pytest tests/ -q
   ```

### 🔵 REFACTOR — Clean Up

8. Improve code quality WITHOUT changing behavior:
   - Extract variables for clarity
   - Remove duplication
   - Improve naming
   - No new functionality

9. Run tests after each refactor step:
   ```bash
   pytest tests/ -q
   ```

10. Repeat from step 2 for the next behavior

## Test Design Principles

**One behavior per test**: Each test asserts exactly one behavior
**Arrange-Act-Assert**:
```python
def test_validate_email_rejects_missing_at_sign():
    # Arrange
    invalid_email = "not-an-email"
    # Act
    result = validate_email(invalid_email)
    # Assert
    assert result is False
```

**Test behaviors, not implementation**:
```python
# ❌ Tests implementation
assert validator._regex.pattern == r"^[^@]+@[^@]+\.[^@]+$"
# ✅ Tests behavior
assert validate_email("user@example.com") is True
assert validate_email("bad-email") is False
```

**Edge cases to always test**:
- Empty input / None
- Boundary values (min/max)
- Invalid types (if not type-checked)
- Already-done state (idempotency)

## KG Search Before Writing

Before writing tests, check for existing patterns:
```
search_code_graph("test patterns for <component type>")
search_knowledge_graph("testing <domain> in Python")
```

## Output Format

After completing the TDD cycle:

```
## TDD Summary

**Feature**: <what was implemented>
**Tests added**: N
  - test_<name>: <what it covers>
  - test_<name>: <what it covers>
**All tests**: ✅ N passing

**Refactors applied**: <what was cleaned up, or "none needed">
```
