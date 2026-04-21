# Effort Estimation Techniques

## 1. Three-Point Estimate (PERT)

**Formula**: Expected = (Optimistic + 4×Most Likely + Pessimistic) / 6

**When to use**: Uncertain tasks, new technologies, complex requirements

**Example**:
```
Task: "Implement payment processing integration"

Optimistic (O):  2 hours (API well-documented, no issues)
Most Likely (M): 4 hours (some debugging needed)
Pessimistic (P): 8 hours (API issues, need support)

Expected = (2 + 4×4 + 8) / 6 = 4.7 hours ≈ 5 hours
```

**Standard Deviation**: σ = (P - O) / 6 = (8 - 2) / 6 = 1 hour
- 68% confidence: 5 ± 1 hours (4-6 hours)
- 95% confidence: 5 ± 2 hours (3-7 hours)

---

## 2. Planning Poker (Team Estimation)

**When to use**: Team environment, collaborative estimation

**Process**:
1. Each member estimates independently (Fibonacci: 1, 2, 3, 5, 8, 13)
2. Reveal estimates simultaneously
3. Discuss outliers (why high? why low?)
4. Re-estimate until consensus

**Example**:
```
Task: "Build user authentication system"

Round 1: [3, 5, 8, 13]
Discussion: "I said 13 because I've never used JWT before"
           "I said 3 because we have a boilerplate"

Round 2: [5, 5, 8, 5]
Consensus: 5 story points ≈ 5 hours
```

**Benefits**: Surfaces assumptions, knowledge sharing, team buy-in

---

## 3. Historical Data (Reference Class Forecasting)

**When to use**: Similar tasks done before

**Process**:
1. Find similar past tasks
2. Review actual time taken
3. Adjust for differences

**Example**:
```
Task: "Add OAuth login (Google)"

Similar past tasks:
- GitHub OAuth: Estimated 3h, Actual 4h (+33%)
- Twitter OAuth: Estimated 4h, Actual 5h (+25%)

Average overrun: ~30%

New estimate: 4h × 1.3 = 5.2 hours ≈ 5-6 hours
```

**Calibration**: Track estimates vs actuals to improve over time

---

## 4. Decomposition (Bottom-Up)

**When to use**: Complex tasks needing granular breakdown

**Process**:
1. Break task into smallest sub-tasks
2. Estimate each sub-task
3. Sum estimates

**Example**:
```
Task: "User authentication system"

Sub-tasks:
1. User model (email, password_hash)       30 min
2. Database migration                      15 min
3. Password hashing utility (bcrypt)       45 min
4. Register endpoint (validation, DB)      1.5 hours
5. Login endpoint (verify, generate JWT)   1 hour
6. JWT utilities (generate, validate)      1 hour
7. Auth middleware (protect routes)        1 hour
8. Unit tests                              1 hour
9. Integration tests                       1.5 hours

Total: 8.25 hours ≈ 8-9 hours
```

**Benefits**: More accurate (smaller estimates easier), surfaces sub-tasks

---

## 5. T-Shirt Sizing (Rough Estimation)

**When to use**: Early planning, backlog grooming, quick prioritization

**Sizes**:
- XS: <1 hour
- S: 1-2 hours
- M: 2-4 hours
- L: 4-8 hours
- XL: >8 hours (needs breakdown)

**Example**:
```
Tasks:
- "Fix typo in docs"                    XS
- "Add validation to form"              S
- "Implement user profile page"         M
- "Build admin dashboard"               L
- "Migrate to new framework"            XL (break down further)
```

**Usage**: Convert to hours when ready to implement
- XS = 0.5h, S = 1.5h, M = 3h, L = 6h

---

## 6. Estimation Factors (Adjustments)

**Complexity**:
- Simple CRUD: 1x
- Business logic: 1.5x
- Complex algorithm: 2-3x
- Novel research: 3-5x

**Unknowns**:
- Well-known tech: 1x
- New library: 1.3x
- New language/framework: 2x
- Unclear requirements: 2-3x

**Team Experience**:
- Expert: 0.5x
- Experienced: 1x
- Junior: 1.5-2x
- Beginner: 3x

**Dependencies**:
- Independent: 1x
- Few dependencies: 1.2x
- Many blockers: 1.5-2x

**Example Adjustment**:
```
Base task: "Implement search feature" = 4 hours

Adjustments:
- New to Elasticsearch: ×1.3
- Junior developer: ×1.5
- Waiting for API access: ×1.2

Adjusted: 4 × 1.3 × 1.5 × 1.2 = 9.4 hours ≈ 10 hours
```

---

## 7. Common Pitfalls

**Optimism Bias**:
- Tendency to underestimate
- Mitigation: Use pessimistic scenario, add buffer

**Anchoring**:
- First estimate influences others
- Mitigation: Independent estimation (Planning Poker)

**Scope Creep**:
- Requirements expand during implementation
- Mitigation: Define done criteria, track changes

**Unknown Unknowns**:
- Unexpected issues not in estimate
- Mitigation: 20-30% buffer for complex tasks

---

## 8. Buffer Strategies

**Task-Level Buffer** (20-30%):
```
Estimated: 8 hours
With buffer: 8 × 1.25 = 10 hours
```

**Project-Level Buffer** (Critical Chain):
```
Tasks: 10h + 8h + 6h = 24 hours (sum of estimates)
Project buffer: 24 × 0.5 = 12 hours (50% of sum)
Total: 36 hours
```

**When to use which**:
- Task buffer: Individual tasks, daily work
- Project buffer: Project timeline, team coordination

---

## 9. Tracking & Calibration

**Log estimates vs actuals**:
```
Task              | Estimated | Actual | Variance
Auth system       | 8h        | 10h    | +25%
Search feature    | 6h        | 9h     | +50%
Profile page      | 4h        | 3.5h   | -12%

Average variance: +21%
```

**Adjust future estimates**:
- If consistently over: Multiply by 1.2
- If consistently under: Multiply by 0.8

**Team calibration**:
- Review estimates quarterly
- Discuss patterns (why over? why under?)
- Improve estimation skills over time
