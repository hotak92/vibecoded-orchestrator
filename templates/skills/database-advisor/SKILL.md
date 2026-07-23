---
name: database-advisor
description: Expert guidance on database design, schema optimization, query performance, and database technology selection. Use when designing a schema from scratch, choosing between SQL/NoSQL/graph/vector databases, fixing a slow query, deciding on indexes, weighing normalize vs denormalize, or planning to scale a database under load. Not for writing a single simple CRUD query.
short_desc: "DB choice, schema design, indexing, query perf"
keywords: ["database schema", "query performance", "index design", "query optimization", "pick a database", "which database", "schema design", "slow query", "optimize query"]
model: sonnet
---

# Database Advisor (Sonnet)

Expert guidance on database design, schema optimization, query performance, and database technology selection.

## Decision Tree

```
Database work involves:
├─ Designing schema from scratch? → Use this skill
├─ Choosing database technology? → Use this skill
├─ Performance optimization needed? → Use this skill
├─ Just writing simple query? → Don't use this skill
└─ Schema already designed? → Use for optimization only
```

## Usage

```
/database-advisor schema-design [application] [requirements]
/database-advisor database-selection [use case] [scale]
/database-advisor query-optimization [slow query]
/database-advisor index-strategy [access patterns]
/database-advisor scaling-plan [current bottleneck]
```

## What This Skill Does

### 1. Database Technology Selection

Compares databases:
- **PostgreSQL**: Structured data, ACID transactions, complex queries
- **MongoDB**: Flexible schema, rapid iteration, hierarchical data
- **Redis**: Caching, sessions, pub/sub, real-time (<1ms latency)
- **Cassandra**: Massive writes, multi-datacenter, time-series
- **Neo4j**: Graph data, relationship queries
- **Weaviate/Pinecone**: Vector search, RAG, semantic search

### 2. Schema Design Principles

- **Normalization**: 1NF, 2NF, 3NF (reduce redundancy, ensure integrity)
- **Denormalization**: Optimize for reads (duplicate data, precompute aggregates)
- **Tradeoffs**: Normalization (integrity, update ease) vs Denormalization (read performance)

### 3. Index Strategy

**When to index**:
- Columns in WHERE clauses
- JOIN conditions
- ORDER BY / GROUP BY columns
- Foreign keys

**Index types**:
- B-Tree (default): Range queries, sorting
- Hash: Equality only
- GiST/GIN: Full-text, JSON, arrays
- Partial: Subset of rows
- Composite: Multiple columns (filter_col, sort_col)

### 4. Query Optimization Techniques

- **Avoid N+1 queries**: Use JOINs instead of multiple queries
- **Use indexes**: Profile with EXPLAIN ANALYZE
- **Limit result sets**: Filter in database, not application
- **Avoid SELECT ***: Fetch only needed columns
- **Connection pooling**: Reuse connections

### 5. Scaling Strategies

- **Vertical**: Bigger server (simple, limits at ~256GB RAM)
- **Read Replicas**: Scale reads (90%+ read workloads)
- **Sharding**: Scale writes (>100K QPS, complex)
- **Caching**: Redis layer (reduces DB load 60-80%)

## Quick Workflow Reference

**Before implementing**: search for proven patterns.
```bash
.claude/scripts/kg-search search "database" --type concept
```

**For deep research**: run `hybrid_search("<database design topic>")` (Weaviate MCP).

## Integration with Knowledge Graph

After database design:
1. Document schema in `knowledge/databases/[project]-schema.md`
2. Link to database technology node
3. Capture optimization patterns
4. Tag with scale tier and technology

## Success Metrics

This skill is working well if:
- ✅ Schema supports all use cases without major revisions
- ✅ Queries perform within target latency (<100ms typical)
- ✅ Database scales to expected load
- ✅ Indexes are effective (high usage, low overhead)
- ✅ Technology choice fits requirements and budget
- ✅ Denormalization decisions are justified and beneficial

