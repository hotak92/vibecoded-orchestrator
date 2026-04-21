# React State Management Patterns

## When to Use Each State Management Solution

| Solution | Use When | Pros | Cons |
|----------|----------|------|------|
| **useState** | Component-local state | Simple, built-in | Doesn't scale |
| **Context API** | App-wide state (<5 contexts) | No dependencies, simple | Re-render issues |
| **Zustand** | Medium apps, less boilerplate | Simple API, minimal re-renders | Smaller ecosystem |
| **Redux Toolkit** | Large apps, complex state | DevTools, proven, ecosystem | Boilerplate, learning curve |
| **Jotai** | Atomic state, granular updates | Minimal re-renders, simple | Newer, less mature |
| **Recoil** | Facebook-scale apps | Concurrent mode ready | Complex API |

---

## Context API Pattern

**Use for**: Theme, auth, app-wide settings

```jsx
// Create context
const ThemeContext = React.createContext();

// Provider
function ThemeProvider({ children }) {
  const [theme, setTheme] = useState('light');

  const toggleTheme = () => {
    setTheme(theme === 'light' ? 'dark' : 'light');
  };

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

// Consumer hook
function useTheme() {
  return useContext(ThemeContext);
}

// Usage
function App() {
  return (
    <ThemeProvider>
      <Header />
      <Content />
    </ThemeProvider>
  );
}

function Header() {
  const { theme, toggleTheme } = useTheme();
  return <button onClick={toggleTheme}>Current: {theme}</button>;
}
```

**Pros**:
- No dependencies
- Built-in to React
- Simple for basic use cases

**Cons**:
- Re-renders all consumers when any value changes
- Can get messy with many contexts
- Performance issues if not optimized

---

## Zustand Pattern

**Use for**: Medium-sized apps, global state without boilerplate

```jsx
import create from 'zustand';

// Create store
const useStore = create((set) => ({
  count: 0,
  user: null,
  increment: () => set((state) => ({ count: state.count + 1 })),
  setUser: (user) => set({ user }),
}));

// Usage (no Provider needed!)
function Counter() {
  const count = useStore((state) => state.count);  // Subscribe to count only
  const increment = useStore((state) => state.increment);

  return <button onClick={increment}>{count}</button>;
}

function UserProfile() {
  const user = useStore((state) => state.user);  // Subscribe to user only
  return <div>{user?.name}</div>;
}
```

**Pros**:
- Simple API (no Provider, no reducers)
- Minimal re-renders (only components using changed state)
- Small bundle size
- DevTools support

**Cons**:
- Less mature ecosystem than Redux
- Fewer middleware options

---

## Redux Toolkit Pattern

**Use for**: Large apps, time-travel debugging, complex state

```jsx
import { configureStore, createSlice } from '@reduxjs/toolkit';
import { Provider, useSelector, useDispatch } from 'react-redux';

// Create slice
const counterSlice = createSlice({
  name: 'counter',
  initialState: { value: 0 },
  reducers: {
    increment: (state) => { state.value += 1 },  // Immer makes this safe
    decrement: (state) => { state.value -= 1 },
  },
});

// Create store
const store = configureStore({
  reducer: {
    counter: counterSlice.reducer,
  },
});

// Usage
function App() {
  return (
    <Provider store={store}>
      <Counter />
    </Provider>
  );
}

function Counter() {
  const count = useSelector((state) => state.counter.value);
  const dispatch = useDispatch();

  return (
    <div>
      <button onClick={() => dispatch(counterSlice.actions.decrement())}>-</button>
      <span>{count}</span>
      <button onClick={() => dispatch(counterSlice.actions.increment())}>+</button>
    </div>
  );
}
```

**Pros**:
- Redux DevTools (time travel, state inspection)
- Huge ecosystem (middleware, async handling)
- Redux Toolkit reduces boilerplate significantly
- Industry standard, well-tested

**Cons**:
- More boilerplate than Zustand
- Steeper learning curve
- Can be overkill for small apps

---

## Decision Flow

```
How complex is your state?
│
├─ Single component → useState
│
├─ 2-3 components → Lift state up or useContext
│
├─ App-wide, simple (theme, auth) → Context API
│
├─ Medium app, multiple features → Zustand
│
└─ Large app, complex workflows → Redux Toolkit
```

**Additional factors**:
- Need time-travel debugging? → Redux Toolkit
- Team already knows Redux? → Redux Toolkit
- Want simplest solution? → Zustand
- Pure client-side, no dependencies? → Context API

---

## Performance Considerations

**Context API Performance Gotcha**:
```jsx
// BAD: Everything re-renders on any state change
const value = { theme, user, settings };  // New object every render
<AppContext.Provider value={value}>

// GOOD: Split into multiple contexts
<ThemeContext.Provider value={theme}>
  <UserContext.Provider value={user}>
    <SettingsContext.Provider value={settings}>
```

**Zustand Performance**: Automatic, subscribes only to used state

**Redux Performance**: Use `useSelector` with specific selectors
```jsx
// BAD: Re-renders on any state change
const state = useSelector(state => state);

// GOOD: Re-renders only when count changes
const count = useSelector(state => state.counter.value);
```

---

## Server State (React Query / SWR)

For server data (API responses), use specialized libraries:

**React Query**:
```jsx
import { useQuery } from 'react-query';

function Users() {
  const { data, isLoading } = useQuery('users', fetchUsers);

  if (isLoading) return <Spinner />;
  return <UserList users={data} />;
}
```

**Benefits over Context/Zustand/Redux for server data**:
- Automatic caching
- Background refetching
- Stale-while-revalidate
- Optimistic updates
- Deduplication

**Use**:
- React Query/SWR for server state (API data)
- Context/Zustand/Redux for client state (UI state, user preferences)
