# Dependency Mapping Patterns

## Dependency Types

### 1. Sequential Dependencies (A → B → C)

**Pattern**: Task B requires Task A to complete first

**Example**:
```
Database Schema → Model Implementation → API Endpoints → Tests
```

**Visualization**:
```
┌──────────────┐
│   Schema     │ Day 1
└──────┬───────┘
       ▼
┌──────────────┐
│   Models     │ Day 2
└──────┬───────┘
       ▼
┌──────────────┐
│  API Routes  │ Day 3
└──────┬───────┘
       ▼
┌──────────────┐
│    Tests     │ Day 4
└──────────────┘

Total: 4 days (cannot parallelize)
```

**When to use**: Foundation → Implementation → Validation workflows

---

### 2. Parallel Dependencies (Independent)

**Pattern**: Tasks can work simultaneously

**Example**:
```
Frontend Components  ┐
Backend API          ├─→ Can work in parallel
Database Schema      ┘
```

**Visualization**:
```
Day 1-2:
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  Frontend   │   │   Backend   │   │  Database   │
└─────────────┘   └─────────────┘   └─────────────┘

Total: 2 days (3 people working in parallel)
```

**When to use**: Independent work streams, different team members

---

### 3. Convergent Dependencies (A, B → C)

**Pattern**: Multiple tasks must complete before next

**Example**:
```
User Model    ┐
Product Model ├─→ Order Model (needs both)
Order Model   ┘
```

**Visualization**:
```
┌─────────────┐
│  User Model │
└──────┬──────┘
       │
       ├──────────┐
       │          ▼
       │   ┌─────────────┐
       │   │ Order Model │
       │   └─────────────┘
       ▼          ▲
┌─────────────┐   │
│Product Model│───┘
└─────────────┘
```

**When to use**: Integration points, dependencies on multiple inputs

---

### 4. External Blockers

**Pattern**: Waiting on external resource

**Example**:
```
Task: "Implement payment flow"
Blocker: "Need Stripe API key from finance team"
```

**Handling**:
1. **Identify early**: List blockers upfront
2. **Request immediately**: Don't wait to start
3. **Mock/stub**: Use fake data while waiting
4. **Parallel work**: Do non-blocked tasks first

**Example Workflow**:
```
Day 1: Request API key, implement UI (not blocked)
Day 2: Implement backend with mock data
Day 3: (API key arrives) Replace mock with real API
```

---

## Dependency Mapping Techniques

### Critical Path Analysis

**Critical Path**: Longest sequence of dependent tasks (determines project duration)

**Example**:
```
Path 1: Schema → Models → API → Tests = 10 hours
Path 2: Frontend Components = 4 hours
Path 3: Documentation = 2 hours

Critical path: Path 1 (10 hours)
Project minimum duration: 10 hours
```

**Optimization**: Focus on critical path tasks first
- Parallelize non-critical work
- Reduce critical path task duration

---

### Dependency Matrix

**When to use**: Complex projects with many tasks

**Example**:
```
       │ A │ B │ C │ D │ E
───────┼───┼───┼───┼───┼───
Task A │ - │   │   │   │
Task B │ X │ - │   │   │   (B depends on A)
Task C │ X │   │ - │   │   (C depends on A)
Task D │   │ X │ X │ - │   (D depends on B, C)
Task E │   │   │ X │   │ - (E depends on C)

Legend: X = depends on
```

**Reading**: Row B depends on column A (B needs A to complete first)

---

### Gantt Chart (Timeline View)

**Example**:
```
Week 1         Week 2         Week 3
|─────────────|─────────────|─────────────|

Schema     [████]
Models           [████]
API                    [████]
Tests                        [████]
Frontend [██████████]
Docs           [████]
```

**Benefits**: Visual timeline, identify overlaps, resource allocation

---

## Dependency Resolution Strategies

### 1. Stub/Mock Pattern

**Problem**: Backend API not ready, frontend blocked

**Solution**: Create mock API

```javascript
// Mock API (Day 1-2, unblocks frontend)
const mockAPI = {
  getUsers: () => Promise.resolve([
    { id: 1, name: 'John' },
    { id: 2, name: 'Jane' }
  ])
};

// Real API (Day 3+, replace mock)
const realAPI = {
  getUsers: () => fetch('/api/users').then(r => r.json())
};
```

**Benefits**: Parallel work, faster iteration

---

### 2. Interface-First Pattern

**Problem**: Teams need to integrate, interfaces unclear

**Solution**: Define contracts first

```typescript
// Define interface (Day 1, all teams agree)
interface UserService {
  getUser(id: string): Promise<User>;
  createUser(data: UserData): Promise<User>;
}

// Team A implements (Day 2-3)
class BackendUserService implements UserService { /* ... */ }

// Team B uses (Day 2-3, can work in parallel)
class FrontendUserClient implements UserService { /* ... */ }
```

---

### 3. Incremental Delivery Pattern

**Problem**: Large feature blocks progress

**Solution**: Break into deployable increments

```
Feature: "User dashboard with analytics"

Increment 1 (MVP):    Basic profile display
Increment 2:          Add activity log
Increment 3:          Add analytics charts
Increment 4:          Add export functionality

Benefits:
- Ship value early
- Reduce risk
- Gather feedback
```

---

## Dependency Anti-Patterns

### 1. Circular Dependencies

**Bad**:
```
Task A depends on Task B
Task B depends on Task A
→ Deadlock!
```

**Fix**: Break cycle, reorder tasks, or combine

---

### 2. Hidden Dependencies

**Bad**: Discover dependency mid-implementation

**Fix**: Thorough upfront analysis, ask "what does this need?"

---

### 3. Over-Serialization

**Bad**: Making tasks sequential when they could be parallel

**Fix**: Identify truly independent work, parallelize

**Example**:
```
BAD:  Schema → Models → API → Frontend → Tests (sequential)
GOOD: Schema → Models → API → Tests
              ↘ Frontend (parallel)
```

---

## Dependency Checklist

Before starting implementation:

- [ ] All task dependencies identified
- [ ] Critical path calculated
- [ ] External blockers requested early
- [ ] Parallel work opportunities identified
- [ ] Mock/stub strategy for blocked tasks
- [ ] Interfaces/contracts defined upfront
- [ ] Incremental delivery plan (if large feature)
- [ ] Team coordination plan (who does what, when)
