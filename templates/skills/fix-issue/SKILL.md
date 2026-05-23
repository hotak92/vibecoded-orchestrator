---
name: fix-issue
description: Investigate and fix a GitHub issue or bug report. Reads the issue, reproduces it, identifies root cause, implements fix, adds regression test.
keywords: ["GitHub issue", "bug report", "regression test", "root cause", "fix bug", "fix this bug", "bug fix", "resolve issue", "debug and fix", "repro the bug", "reproduce the issue"]
argument-hint: "[issue-url-or-description]"
model: sonnet
---

# /fix-issue [issue-number or description]

Structured workflow for investigating and fixing a reported bug or GitHub issue.

## Usage

```
/fix-issue 42                     # Fix GitHub issue #42 (reads via gh CLI)
/fix-issue "auth fails on logout" # Fix by description
/fix-issue                        # Fix current known bug (from CONTEXT_STATE.md)
```

## Workflow

### 1. Understand the Issue

If issue number provided:
```bash
gh issue view <number>
```
Otherwise, read description from the prompt or CONTEXT_STATE.md.

Extract:
- **Expected** behavior
- **Actual** behavior
- **Reproduction steps**
- **Affected files** (if known)

### 2. Search for Root Cause

**KG-first**: `search_knowledge_graph("error description or symptom")`
**Code graph**: `search_code_graph("function or component name")`
**Then Grep**: for exact error messages, function names, stack traces

### 3. Reproduce the Bug

Run existing tests to confirm failure:
```bash
pytest tests/ -k "relevant_test" -v
```

If no existing test covers it, write a failing test first (TDD approach).

### 4. Identify Root Cause

Read the relevant files. Trace the execution path. Document root cause in 1-2 sentences before writing any fix.

### 5. Implement the Fix

- Minimal change — don't refactor surrounding code
- No new behavior beyond what's needed for the fix
- Keep diff small and focused

### 6. Add Regression Test

- If a failing test was written in step 3, ensure it now passes
- If no test existed, add one that would have caught the bug
- Test name: `test_<bug_description>_regression`

### 7. Verify

```bash
pytest tests/ -q                  # all pass
pytest tests/ -k "regression"     # new test passes
```

### 8. Optional: Close Issue

```bash
gh issue close <number> --comment "Fixed in <commit>. Added regression test."
```

## Anti-patterns

- Don't fix symptoms, fix root causes
- Don't add workarounds that mask the bug
- Don't change behavior beyond the fix scope

## Output

Summary:
- Root cause: [1 sentence]
- Fix: [what changed]
- Test added: [test name]
- All tests: ✅ passing
