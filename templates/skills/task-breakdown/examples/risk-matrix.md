# Risk Assessment Matrix

## Risk Categories

### 1. Technical Risks

**New Technology**:
- Risk: Team unfamiliar with tool/framework
- Likelihood: High (if truly new)
- Impact: Medium-High (learning curve, potential mistakes)
- Mitigation: Spike tasks, training, pair programming

**Example**:
```
Risk: "Never used GraphQL before"
Likelihood: High
Impact: Medium (slower development, potential mistakes)
Mitigation:
  - 2-hour spike task to learn basics
  - Pair with someone who knows GraphQL
  - Start with simple query, iterate
```

---

**Complex Algorithms**:
- Risk: Algorithm difficult to implement correctly
- Likelihood: Medium (depends on complexity)
- Impact: High (bugs, performance issues)
- Mitigation: Prototyping, extensive testing, code review

**Example**:
```
Risk: "Implementing custom recommendation algorithm"
Likelihood: Medium
Impact: High (incorrect recommendations hurt UX)
Mitigation:
  - Prototype with small dataset first
  - 100% test coverage on core logic
  - A/B test against baseline
```

---

**Integration Challenges**:
- Risk: Third-party API unreliable or poorly documented
- Likelihood: Medium-High
- Impact: High (blocks implementation)
- Mitigation: Test early, have fallback, contact support

**Example**:
```
Risk: "Payment gateway API might have downtime"
Likelihood: Low (99.9% uptime SLA)
Impact: High (no payments = revenue loss)
Mitigation:
  - Implement retry logic with exponential backoff
  - Queue failed payments for later processing
  - Alert on payment failures
```

---

**Performance Requirements**:
- Risk: System too slow at scale
- Likelihood: Medium (depends on load)
- Impact: High (poor UX, customer churn)
- Mitigation: Load testing, profiling, optimization

**Example**:
```
Risk: "Dashboard queries might timeout at 10K users"
Likelihood: Medium
Impact: High (dashboard unusable)
Mitigation:
  - Load test with realistic data
  - Add caching (Redis)
  - Database query optimization (indexes)
  - Pagination if needed
```

---

### 2. Dependency Risks

**External Blockers**:
- Risk: Waiting on other team/person
- Likelihood: Medium-High
- Impact: Medium-High (delays)
- Mitigation: Request early, have parallel work

**Example**:
```
Risk: "Need design approval before implementing UI"
Likelihood: High (designer busy)
Impact: Medium (delays frontend work)
Mitigation:
  - Request designs ASAP (don't wait)
  - Work on backend while waiting
  - Use wireframes if designs delayed
```

---

**Team Availability**:
- Risk: Key person unavailable (vacation, sick, competing priorities)
- Likelihood: Low-Medium
- Impact: Medium-High (knowledge bottleneck)
- Mitigation: Knowledge sharing, documentation, cross-training

**Example**:
```
Risk: "Only Alice knows the auth system, she's on vacation next week"
Likelihood: High (vacation scheduled)
Impact: High (auth bugs can't be fixed)
Mitigation:
  - Alice documents auth system before vacation
  - Pair with Alice this week (knowledge transfer)
  - Avoid auth changes during vacation
```

---

**Third-Party Services**:
- Risk: API downtime, rate limits, breaking changes
- Likelihood: Low-Medium
- Impact: Medium-High (service unavailable)
- Mitigation: Fallbacks, monitoring, version pinning

**Example**:
```
Risk: "Email service rate limit (100 emails/hour)"
Likelihood: Medium (at scale)
Impact: Medium (some emails fail)
Mitigation:
  - Queue emails with retry logic
  - Upgrade plan if hitting limits
  - Monitor email send rate
```

---

### 3. Scope Risks

**Unclear Requirements**:
- Risk: Ambiguous specs lead to rework
- Likelihood: High (common issue)
- Impact: High (wasted effort)
- Mitigation: Clarify before coding, iterative delivery

**Example**:
```
Risk: "'User profile' unclear - what fields? editing? privacy?"
Likelihood: High
Impact: High (might build wrong thing)
Mitigation:
  - List all fields, get approval
  - Create mockup, get feedback
  - Start with MVP, iterate
```

---

**Scope Creep**:
- Risk: Requirements expand during implementation
- Likelihood: High
- Impact: Medium-High (timeline slips)
- Mitigation: Define done criteria, track changes, say no

**Example**:
```
Risk: "User requests 'one more feature' mid-sprint"
Likelihood: High
Impact: Medium (delays original scope)
Mitigation:
  - Define MVP strictly, document it
  - New requests go to backlog (next sprint)
  - Track scope changes, communicate timeline impact
```

---

**Hidden Complexity**:
- Risk: Task appears simple but has hidden gotchas
- Likelihood: Medium
- Impact: Medium-High (timeline surprise)
- Mitigation: Spike tasks, decomposition, ask experts

**Example**:
```
Risk: "'Add CSV export' sounds simple, but 100K rows crashes browser"
Likelihood: Medium (discovered during implementation)
Impact: Medium (need different approach)
Mitigation:
  - Ask: "What's the max size?" upfront
  - Spike with large dataset first
  - Use server-side export if needed
```

---

## Risk Matrix Template

| Risk | Likelihood | Impact | Priority | Mitigation |
|------|------------|--------|----------|------------|
| API integration fails | Medium | High | HIGH | Test early, have mock fallback |
| Performance bottleneck | Low | Medium | MEDIUM | Load test, optimize if needed |
| Unclear requirements | High | High | HIGH | Clarify with user before coding |
| Designer unavailable | Medium | Low | LOW | Use wireframes, iterate later |
| Third-party downtime | Low | High | MEDIUM | Implement retry logic, monitoring |

**Priority Calculation**:
- HIGH: High likelihood × High impact
- MEDIUM: Either high likelihood OR high impact
- LOW: Low likelihood × Low impact

---

## Risk Mitigation Strategies

### 1. Prototyping (Spike Tasks)

**When**: New technology, uncertain approach, complex algorithm

**Process**:
1. Time-box (2-4 hours max)
2. Build minimal proof-of-concept
3. Answer key question ("Can we do X with Y?")
4. Throw away code (it's a spike, not production)

**Example**:
```
Risk: "Not sure if Weaviate can handle our query volume"
Spike: 2-hour test with realistic data volume
Outcome: Yes, can handle it → Proceed with confidence
```

---

### 2. Incremental Delivery (MVP First)

**When**: Large feature, unclear requirements, high uncertainty

**Process**:
1. Define Minimum Viable Product (MVP)
2. Ship MVP early (validate assumptions)
3. Iterate based on feedback
4. Add features incrementally

**Example**:
```
Feature: "Analytics dashboard"

MVP (Week 1):     Show basic metrics (users, revenue)
Iteration 2:      Add charts
Iteration 3:      Add date filters
Iteration 4:      Add export

Benefits:
- Ship value early (Week 1, not Week 4)
- Validate with real users
- Adjust based on feedback
```

---

### 3. Parallel Work (Don't Wait)

**When**: External blockers, dependencies on other teams

**Process**:
1. Identify non-blocked work
2. Start non-blocked work immediately
3. Use mocks/stubs for blocked parts
4. Integrate when blocker resolved

**Example**:
```
Blocked: "Backend API not ready"
Parallel work:
  - Build frontend with mock API
  - Write tests with mock data
  - Design UI components
When API ready: Replace mock with real API (quick)
```

---

### 4. Fallback Plans (Plan B)

**When**: Critical paths, external dependencies, uncertain solutions

**Process**:
1. Identify critical risks
2. Define Plan B if Plan A fails
3. Document trigger conditions

**Example**:
```
Plan A: Use GraphQL for flexible queries
Plan B: REST API (if GraphQL too complex)
Trigger: If GraphQL spike takes >4 hours or looks risky

Plan A: Real-time WebSockets
Plan B: Polling every 5 seconds
Trigger: If WebSocket implementation blocked
```

---

## Risk Assessment Checklist

Before starting implementation:

**Technical**:
- [ ] Team has required skills (or training plan)
- [ ] Technology proven in similar context
- [ ] Performance requirements realistic
- [ ] Complex parts prototyped/spiked

**Dependencies**:
- [ ] External resources requested early
- [ ] Team availability confirmed
- [ ] Third-party services tested
- [ ] Fallbacks defined for critical paths

**Scope**:
- [ ] Requirements clarified and documented
- [ ] MVP/done criteria defined
- [ ] Scope change process agreed
- [ ] Hidden complexity investigated

**Mitigation**:
- [ ] High-priority risks have mitigation plans
- [ ] Spike tasks scheduled upfront
- [ ] Parallel work identified
- [ ] Fallback plans documented

---

## Risk Review Cadence

**Daily** (during implementation):
- Are we encountering new risks?
- Are mitigations working?

**Weekly** (sprint planning):
- Review risk matrix
- Update likelihood/impact
- Add new risks discovered

**Per-Project** (retrospective):
- Which risks materialized?
- Which mitigations worked?
- What did we miss?
- How to improve risk assessment?
