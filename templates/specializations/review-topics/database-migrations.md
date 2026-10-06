# Database migrations review topic

Lens for reviewing schema migrations and data-migration code. Findings carry
severity, `file:line`, and evidence.

## Compatibility sequence

Safe migrations keep old and new code working against the database during
the rollout window:

1. **Additive first** — new table/column/index deployed before the code that
   uses it; new columns nullable or with defaults.
2. **Dual-write** — during transition, write old and new shapes in one
   transaction.
3. **Backfill** — batched (bounded batch size, pause between batches),
   idempotent, resumable after interruption.
4. **Verify** — row counts match plus sampled full-row comparison BEFORE
   switching reads.
5. **Switch reads** — behind a flag where possible.
6. **Drop old** — only after a grace period long enough to roll back.

A migration that requires new code to already be deployed everywhere, or old
code to already be gone, is a finding.

## Rollback

- Every migration names its rollback path; destructive steps (DROP,
  truncating backfill) are separated from additive steps so rollback never
  needs to recreate dropped data.
- Rollback is tested at least once in a non-production environment — an
  untested rollback plan is UNVERIFIED and must be labeled so.

## Locking & load

- Long locks on hot tables called out: which statements take
  ACCESS EXCLUSIVE-class locks, for how long, at what table size.
- Index creation on large tables uses the concurrent/non-blocking form where
  the engine provides one.
- Backfill load bounded; runs outside peak or throttled; monitored.

## Data integrity

- Constraints (NOT NULL, UNIQUE, FK, CHECK) added only after backfill made
  them satisfiable; constraint validation split from constraint addition
  where the engine supports it.
- Type changes: explicit cast behavior for every existing value shape;
  money stays exact (numeric/decimal), never float.
- No data destroyed by a migration that was not positively confirmed
  redundant — seeded/backup metadata never gates destruction on its own.

## Operations

- Migration order deterministic; no cross-dependencies on uncommitted
  migrations.
- Migrations are idempotent or guarded (`IF NOT EXISTS` class) where the
  tooling allows re-runs.
- The deploy plan states: migration first or code first, flag states at each
  step, and the abort criteria.
