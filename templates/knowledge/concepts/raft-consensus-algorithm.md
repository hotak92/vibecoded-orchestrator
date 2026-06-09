---
title: Raft Consensus Algorithm
type: concept
tags: [distributed-systems, consensus, fault-tolerance, replication, algorithms, low-level-implementation]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:33:47Z
status: active
---

# Raft Consensus Algorithm

## Overview

Raft is a consensus algorithm designed as an understandable alternative to Paxos, introduced by Diego Ongaro and John Ousterhout in 2014 ("In Search of an Understandable Consensus Algorithm," USENIX ATC). It provides a way to distribute a replicated state machine across a cluster, ensuring all nodes agree on the same sequence of state transitions. The name is a backronym: Reliable, Replicated, Redundant, And Fault-Tolerant.

Unlike Paxos, which is famously difficult to reason about due to its symmetric peer-to-peer approach, Raft achieves understandability by decomposing consensus into three largely independent subproblems: leader election, log replication, and safety. Raft is not Byzantine fault tolerant — nodes trust the elected leader unconditionally.

## Core Mechanics

### Node Roles
- **Leader**: Handles all client requests; replicates log entries to followers; sends periodic heartbeats
- **Follower**: Passive; responds to RPCs from leader and candidates; becomes candidate on election timeout
- **Candidate**: Transitional role during elections; votes for itself and solicits votes

### Leader Election
Raft uses randomized election timeouts (typically 150–300 ms) to avoid split votes. Each election occurs within a **term** — a monotonically increasing integer that acts as a logical clock. A follower that receives no heartbeat within its timeout becomes a candidate:
1. Increments current term, votes for itself
2. Broadcasts RequestVote RPC to all peers
3. A node votes at most once per term (first-come-first-served)
4. Candidate wins if it receives votes from a majority (quorum = ⌊N/2⌋ + 1)
5. If no majority forms, a new term begins with a fresh election

Voters reject candidates whose logs are less up-to-date than their own (compared by last entry's term, then log length). This prevents stale leaders.

### Log Replication
1. Client sends command to leader
2. Leader appends entry to its local log with current term number
3. Leader sends AppendEntries RPC (also doubles as heartbeat) to all followers in parallel
4. Once a majority acknowledges the entry, it is **committed**
5. Leader applies committed entry to state machine, returns result to client
6. Leader notifies followers of committed index in subsequent AppendEntries; followers apply entries

Uncommitted entries from a crashed leader are overwritten: the new leader forces followers to match its log by finding the last matching entry and replacing all subsequent follower entries.

### Safety Properties (formally proven)
- **Election Safety**: At most one leader per term
- **Leader Append-Only**: Leaders never overwrite or delete their own log entries
- **Log Matching**: If two logs have the same (index, term) entry, they are identical up to that point
- **Leader Completeness**: If an entry is committed in term T, it exists in all leaders elected in terms > T
- **State Machine Safety**: No two servers apply different commands at the same log index

### Membership Changes
Raft handles cluster reconfiguration (adding/removing nodes) via joint consensus — the cluster briefly operates under both old and new configurations simultaneously, ensuring majority is maintained throughout.

## Comparison with Paxos

| Aspect | Raft | Paxos (Multi-Paxos) |
|---|---|---|
| Design goal | Understandability | Correctness first |
| Leadership | Strong single leader | Leader often implicit |
| Log gaps | Never allowed | Possible (holes) |
| Reconfiguration | Joint consensus | Ad hoc, complex |
| Formal proofs | Yes (Coq, TLA+) | Yes, but harder to verify |

Both achieve the same fault tolerance: a cluster of 2f+1 nodes tolerates f failures. Both are CFT (Crash Fault Tolerant), not BFT.

## Practical Deployments

- **etcd**: Kubernetes' primary key-value store uses Raft for cluster coordination
- **CockroachDB**: Uses Raft per shard for multi-range distributed transactions
- **TiKV** (TiDB's storage layer): Raft-based distributed key-value
- **Consul**: Service discovery and health checking via Raft
- **InfluxDB**: Uses Raft for metadata storage in clustered mode
- **RethinkDB** (historical): Early adopter of Raft

## Performance Characteristics

- **Write latency**: At minimum one round-trip (leader → followers → leader commit → client)
- **Read latency**: Reads can be served from leader with lease-based optimizations; followers serve stale reads
- **Throughput**: Leader is bottleneck; pipelining AppendEntries helps; typical deployments handle 10K–100K ops/sec
- **Leader failover**: Typically 150–500 ms (dependent on heartbeat interval and election timeout)

## Tradeoffs

**Strengths**:
- Strong consistency (linearizability) by default
- Readable spec and reference implementations (raft.github.io)
- Simpler operational model than Paxos

**Weaknesses**:
- Leader is a single point of throughput bottleneck
- Not Byzantine fault tolerant (use PBFT/Tendermint for adversarial settings)
- Joint consensus membership changes add operational complexity
- Followers cannot serve linearizable reads without lease extensions

## Key Papers
- Ongaro & Ousterhout, "In Search of an Understandable Consensus Algorithm," USENIX ATC 2014
- Ongaro, "Consensus: Bridging Theory and Practice" (PhD thesis, Stanford, 2014)
- Evrard (2020) — formal verification in LNT process algebra
- Bora et al. (2024) — formal verification in mCRL2

## Links

[[relatedTo::Byzantine Fault Tolerance]]
[[relatedTo::Paxos Consensus]]
[[implements::Replicated State Machine]]
[[relatedTo::etcd]]
[[relatedTo::Distributed Consensus]]
