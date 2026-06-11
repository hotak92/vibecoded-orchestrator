---
name: database-advisor
description: Expert guidance on database design, schema optimization, query performance, and database technology selection
short_desc: "DB choice, schema design, indexing, query perf"
keywords: ["database schema", "query performance", "index design", "query optimization", "pick a database", "which database", "schema design", "slow query", "optimize query"]
model: sonnet
---

# Database Advisor (Sonnet)

**Purpose**: Expert guidance on database design, schema optimization, query performance, and database technology selection.

**Model**: Sonnet 4.5 (balanced reasoning for database architecture decisions)

**When to Invoke Autonomously**:

Use this skill when:
1. **Schema Design**: "Design database schema for [application]"
2. **Database Selection**: "SQL, NoSQL, graph, or vector database for [use case]?"
3. **Query Optimization**: "Query too slow, how to optimize?"
4. **Index Strategy**: "Which indexes to create for [access patterns]?"
5. **Normalization Decisions**: "Should I normalize or denormalize for [performance/maintainability]?"
6. **Scaling Strategy**: "Database can't handle load, how to scale?"

**DO NOT invoke for**:
- Simple CRUD queries (just write them)
- Already-designed schema (use for optimization only)
- Single table designs

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

For detailed comparisons, see [examples/database-comparison.md](examples/database-comparison.md).

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

## Output Format

See [template.md](template.md) for database design documentation structure.

## Quick Workflow Reference

**Before implementing**: Search for proven patterns
```bash
.claude/scripts/kg-search search "database" --type concepts
```

**For deep research**: Ask user "Use hybrid_search to research [database design]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Integration with Knowledge Graph

After database design:
1. Document schema in `knowledge/databases/[project]-schema.md`
2. Link to database technology node
3. Capture optimization patterns
4. Tag with scale tier and technology

## Supporting Files

- **Template**: Use [template.md](template.md) for database design documentation
- **Examples**: See [examples/database-comparison.md](examples/database-comparison.md) and [examples/schema-examples.md](examples/schema-examples.md)

## Success Metrics

This skill is working well if:
- ✅ Schema supports all use cases without major revisions
- ✅ Queries perform within target latency (<100ms typical)
- ✅ Database scales to expected load
- ✅ Indexes are effective (high usage, low overhead)
- ✅ Technology choice fits requirements and budget
- ✅ Denormalization decisions are justified and beneficial

