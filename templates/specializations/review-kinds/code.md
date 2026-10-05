# Code review kind

What to hunt when reviewing a diff for correctness. Read before a code
review; every finding carries severity, `file:line`, evidence, and a
suggested fix.

## Correctness

- Does the code do what the brief/spec says — for ALL inputs, not the test
  cases? Look for hard-coded values that make tests pass.
- Edge cases: empty, null, zero, negative, boundary, duplicate, concurrent,
  very large.
- Error handling: specific exceptions with context; no swallowed errors; no
  stack traces leaking to users; failure paths leave state consistent
  (rollbacks, cleanup, no partial writes).
- Off-by-one, wrong operator, inverted condition, mutation of shared state.

## Callers and contracts

- Every changed signature: are ALL call sites updated? (grep, don't assume.)
- Shared-component extraction: one un-migrated call site leaves the suite
  green while defeating the change — enumerate call sites in the report.
- Public contracts (APIs, CLIs, file formats, env vars): breaking changes
  versioned and documented, not silently deployed.

## Modularity and duplication

- New helper added while an equivalent exists → duplication finding
  (search before add; one concern, one home).
- Logic inlined at a second call site → extract instead.
- Dead code, unused imports, TODO/FIXME left behind by this diff.

## Promises

- Comments/docstrings describing behavior the code does not have — the
  comment is part of the defect.
- Printed commands that do not exist or do not help.
- Config keys declared but never read; flags that change nothing observable.

## Tests in the diff

- New branch that gates a destructive action (delete/overwrite/kill) has a
  test for BOTH the act and the leave-alone case.
- Tests assert behavior, not implementation trivia; a test that cannot fail
  is a finding.

## Verification discipline

- Verify load-bearing claims IN THE SOURCE — read the code, re-run the repro
  — before endorsing or rejecting. Never relay an unchecked claim.
- State what was NOT reviewed (files, angles) so the requester knows the
  boundary.
- "No findings" is only valid after naming what was checked.
