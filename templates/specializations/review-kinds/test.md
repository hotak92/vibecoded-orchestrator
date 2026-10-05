# Test review kind

What to hunt when reviewing tests and test coverage in a diff. Every finding
carries severity, `file:line`, evidence, and a suggested fix.

## Do the tests validate the shipped behavior?

- Tests must validate the NEW release's code, not a stale copy: check what
  the test imports/installs and whether a shadow copy (an old non-editable
  install, a stale build artifact) could satisfy it instead of the real code.
- Argv-shape tests (asserting a command list was constructed) miss live
  parser rejections — prefer executing the real parser/CLI where feasible.
- Mocks: a mock that encodes an assumption instead of the real contract is a
  finding; mocks that hide absence (asserting on a call that duplicates the
  mock setup) test nothing.
- A test that cannot fail (asserts on its own fixture, tautology, swallowed
  exception) is a finding.

## Coverage of decisions

- Every new branch that gates a destructive action (stop/kill/delete/
  overwrite) has BOTH arms tested: the act case and the leave-alone case.
- Error paths and edge cases (empty, boundary, malformed, concurrent) have
  at least one test where the branch is new.
- Red-proof: for bug fixes, the regression test fails on the pre-fix code and
  passes after — a test written only against fixed code proves nothing about
  the bug.

## Test quality

- Names claim exactly what is asserted — a name promising more than the body
  checks is a false promise.
- Independence: no order dependence, no shared mutable state across tests,
  no reliance on a live external service unless the suite declares it.
- Determinism: time, randomness, and network are controlled or faked;
  flaky-by-design tests are findings.
- Runtime: a suite that silently grew minutes-long is worth a finding when
  the diff caused it.

## Environment divergence

- Tests that pass locally but depend on ambient state (env vars, services on
  default ports, user config) are CI time bombs — require explicit setup or
  sentinels (e.g. an unroutable URL for "must not connect" cases).
- Fixture pollution: tests writing to real backends/collections instead of
  isolated fixtures — check the suite's guard conventions are used.

## What was NOT reviewed

Name the modules/branches you did not check and any test you could not run,
so the requester knows the boundary of the review.
