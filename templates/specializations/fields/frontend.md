# Frontend field guide

Specialisation depth for React-ecosystem frontend work: component patterns,
state management selection, performance, testing. Read this before
structuring complex components, choosing a state solution, or fixing
re-render problems. Principles generalize to Vue/Svelte with framework
equivalents.

## Component patterns

- **Composition** — small, single-purpose components combined via children/
  slots; the default choice.
- **Custom hooks** — extract reusable stateful logic out of components.
- **Compound components** — related groups sharing implicit state
  (Tabs/Tab, Select/Option).
- **Container/presentational split** — logic and data fetching separated
  from rendering.
- **Render props / HOCs** — legacy escape hatches; prefer hooks.

## State management selection

| Solution | Use for |
|---|---|
| `useState`/`useReducer` | Component-local state |
| Context API | App-wide low-churn state (theme, auth); few contexts |
| Zustand-class store | Medium apps, shared client state, low boilerplate |
| Redux Toolkit | Large apps, complex state flows, devtools/time-travel |
| React Query / SWR | Server state (API data): caching, refetch, sync |

Rule: server state belongs in a server-state library, not hand-rolled
`useEffect` + `useState` fetching.

## Performance

- Memoization with evidence: `React.memo`, `useMemo`, `useCallback` only
  where profiling shows wasted renders — not by default.
- Code splitting: route-based lazy loading first.
- Virtualize long lists (react-window class).
- Debounce/throttle high-frequency handlers (input, resize, scroll).
- Production builds only when measuring (minification, tree shaking change
  everything).
- Re-render debugging: find the state that changed and who subscribes to it;
  push state down, lift content up.

## Real user experience standards

- Every async view has loading, empty, and error states — not just success.
- Forms: inline validation, disabled-while-submitting, server-error display.
- Accessibility: semantic HTML first; keyboard navigable; focus management on
  route/modal changes; WCAG 2.1 AA contrast.
- Responsive: verify at mobile/tablet/desktop widths.

## Testing

- Unit: components in isolation (Testing Library + Jest/Vitest class).
- Integration: component interactions and data flows.
- E2E: full user flows (Playwright class) — few, high-value.
- Test behavior users observe, not implementation details.

## Related

- Review lens: `review-topics/frontend-ui-a11y.md`
