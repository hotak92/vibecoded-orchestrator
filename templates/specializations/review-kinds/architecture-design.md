# Architecture / design review kind

What to examine when reviewing a design, plan, or architectural change (as
opposed to line-level code). Findings carry severity, evidence (document or
`file:line`), and a concrete alternative.

## Boundaries and responsibility

- Component boundaries: does each component have one reason to change?
  Name the responsibilities that leaked across a boundary.
- Layering: side-effecting I/O at the edges, pure decision logic in the core
  (unit-testable without services).
- Shared logic: needed at 2+ call sites → one home, called from each;
  mirrors/copies are findings unless locked by a parity test with a
  "must match <other file>" comment.
- Cross-language duplication: shared code (A) > shared config table (B) >
  mirror (C); a fresh mirror where A or B was reachable is a finding.

## Trade-offs and alternatives

- The design names the alternatives it rejected and why — a single-option
  design document is incomplete.
- Trade-offs are explicit (complexity vs performance, lock-in vs
  convenience, time-to-market vs debt), not implied.
- Scale/complexity is proportionate to the actual requirement — flag
  speculative generality as well as under-design.

## Failure modes

- What happens when each dependency is down, slow, or lying (stale data)?
  Best-effort paths fail soft with a log line; load-bearing paths fail loud.
- Missing-dependency behavior: a shipped dependency failing to import/connect
  means a broken install — surfaced, never silently worked around inline.
- Destructive operations: guarded by positive confirmation of preconditions;
  deferred/consented, never auto-applied.

## Evolution and migration

- Migration path from the current state is sequenced, with rollback at each
  step; dual-write/backfill/verify patterns where data moves.
- The design does not paint into a corner: name the extension points and the
  things deliberately NOT abstracted yet.
- Interface contracts (APIs, schemas, file formats, env vars) versioned;
  deprecation timelines stated.

## Consistency with the system

- Fits existing patterns in the codebase (search first — cite the existing
  pattern it follows or explain the deviation).
- Does not contradict a recorded decision without saying so and superseding
  it explicitly.

## Verification discipline

Verify load-bearing claims in the source/documents cited. State which parts
of the design were not examined. "No findings" only after naming what was
checked.
