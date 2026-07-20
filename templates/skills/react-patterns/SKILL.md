---
name: react-patterns
description: React best practices - component patterns, state management selection, performance optimization, testing strategies
short_desc: React component patterns, state, perf, testing strategy
keywords: ["React hooks", useState, useEffect, "component state", useMemo, useCallback, Zustand, "React component", "React state", "React performance", "React testing", Redux, "React Context"]
model: sonnet
---

# React Patterns (Sonnet)

**Purpose**: React best practices - component patterns, state management selection, performance optimization, testing strategies.

**Model**: Sonnet (balanced reasoning for React architecture)

## When to Invoke Autonomously

1. **Component Structure**: "How to structure [complex component]?"
2. **State Management**: "Context, Redux, or Zustand for [use case]?"
3. **Performance**: "Component re-rendering too much, how to optimize?"
4. **Patterns**: "Composition, render props, or custom hooks for [scenario]?"

## DO NOT invoke for

- Simple components (just write them)
- Already-designed architecture
- Non-React frameworks

## Usage

```
/react-patterns component-structure [complex UI]
/react-patterns state-management [app requirements]
/react-patterns performance-optimization [slow component]
/react-patterns testing-strategy [component type]
```

## What This Skill Does

**Component Patterns**:
- Composition (flexible, reusable UI components)
- Custom hooks (extract reusable logic)
- Render props (dynamic rendering)
- Compound components (related component groups)
- Container/presentational split (logic vs UI)
- HOCs (cross-cutting concerns)

**State Management Selection**:
- useState for component-local state
- Context API for app-wide state (<5 contexts)
- Zustand for medium apps (less boilerplate)
- Redux Toolkit for large apps (complex state, DevTools)
- React Query/SWR for server state (API data)

**Performance Optimization**:
- Memoization (React.memo, useMemo, useCallback)
- Code splitting (lazy loading, route-based)
- Virtual scrolling (long lists with react-window)
- Debouncing/throttling (reduce function calls)
- Production builds (minification, tree shaking)

**Testing Strategies**:
- Unit tests (components in isolation)
- Integration tests (component interactions)
- E2E tests (full user flows)
- Testing library recommendations (Jest, React Testing Library)

**See**: `examples/component-patterns.jsx` for pattern code examples, `examples/state-management.md` for state solution comparison, `examples/performance.jsx` for optimization techniques

## Quick Workflow Reference

**Before implementing**: Search for proven React patterns
```bash
.claude/scripts/kg-search search "react" --type concept
```

**For deep research**: Ask user "Use hybrid_search to research [React pattern comparison]"

**Development env**: Python 3.12, Weaviate:8081, Ollama:11435, venv: `source claude_mcp_servers/.venv/bin/activate`

## Success Metrics

- ✅ Components are reusable and maintainable
- ✅ State management fits app complexity
- ✅ Performance targets met (FCP <1.5s, no layout shifts)
- ✅ Pattern choice is justified and appropriate
- ✅ Code follows React best practices
