# Database field guide

Specialisation depth for database design, schema optimization, query
performance, and technology selection. Read this before schema work, choosing
a datastore, fixing slow queries, or planning scale.

## Technology selection

| Store | Fit |
|---|---|
| PostgreSQL / relational | Structured data, ACID transactions, complex queries |
| MongoDB / document | Flexible schema, rapid iteration, hierarchical data |
| Redis | Caching, sessions, pub/sub, sub-millisecond reads |
| Cassandra / wide-column | Massive write throughput, multi-datacenter, time-series |
| Neo4j / graph | Relationship-heavy queries |
| Weaviate / vector stores | Semantic search, RAG |

Decide from: access patterns, consistency needs, write/read ratio, scale
targets, team familiarity, operational cost.

## Schema design

- Normalize (1NF–3NF) to reduce redundancy and protect integrity.
- Denormalize deliberately for read-heavy paths (precomputed aggregates,
  duplicated hot fields) — record WHY in a comment or doc.
- Every table: explicit PK; `created_at`/`updated_at`; constraints at the
  DB level (unique, check, FK) not only in application code.
- Store money as Decimal/numeric, never float. Enforce lowercase email etc.
  via trigger or check constraint where the app relies on it.

## Indexing

Index: WHERE-clause columns, JOIN keys, ORDER BY/GROUP BY columns, foreign
keys. Types: B-tree (ranges/sorting, default), hash (equality), GIN/GiST
(full-text, JSON, arrays), partial (row subsets), composite (filter column
first, then sort column). Verify usage with EXPLAIN ANALYZE; unused indexes
are write amplification.

## Query performance

- Kill N+1 patterns: JOIN or batch-fetch instead of per-row queries.
- `SELECT` only needed columns.
- Filter and paginate in the database, not the application.
- Connection pooling; sane statement timeouts.
- Profile before optimizing — measure, then change one thing.

## Scaling ladder

1. **Vertical** — bigger instance; simplest; ceiling at host limits.
2. **Caching** — Redis layer for hot reads; often 60–80% load reduction.
3. **Read replicas** — for read-dominant workloads; plan replication lag
   handling (sticky reads after writes).
4. **Sharding / partitioning** — for write scale; operationally complex;
   last, not first.

## Migrations

- Backwards-compatible steps: add new → dual-write → backfill in batches →
  verify (row counts + sampled full comparison) → switch reads → drop old
  after a grace period.
- Every migration has a rollback path; never deploy code that requires a
  destructive migration to have already run.

## Related

- Backend implementation standards: `fields/backend.md`
- Review lenses: `review-topics/database-migrations.md`,
  `review-topics/performance.md`
