// React Performance Optimization Examples

// ============================================
// 1. MEMOIZATION - React.memo
// ============================================

// Without memo: Re-renders on every parent render
function ExpensiveComponent({ data }) {
  // Heavy computation or large render
  return <div>{/* ... */}</div>;
}

// With memo: Only re-renders when props change
const ExpensiveComponent = React.memo(function ExpensiveComponent({ data }) {
  return <div>{/* ... */}</div>;
});

// Custom comparison function
const ExpensiveComponent = React.memo(
  function ExpensiveComponent({ data }) {
    return <div>{/* ... */}</div>;
  },
  (prevProps, nextProps) => {
    // Return true if props are equal (skip re-render)
    return prevProps.data.id === nextProps.data.id;
  }
);

// ============================================
// 2. MEMOIZATION - useMemo
// ============================================

function DataTable({ items }) {
  // BAD: Recalculates on every render
  const sortedItems = items.sort((a, b) => a.value - b.value);
  const filteredItems = sortedItems.filter(item => item.active);

  return <Table data={filteredItems} />;
}

function DataTable({ items }) {
  // GOOD: Only recalculates when items change
  const processedItems = useMemo(() => {
    const sorted = items.sort((a, b) => a.value - b.value);
    return sorted.filter(item => item.active);
  }, [items]);

  return <Table data={processedItems} />;
}

// ============================================
// 3. MEMOIZATION - useCallback
// ============================================

function Parent() {
  // BAD: New function on every render (child re-renders unnecessarily)
  const handleClick = () => console.log('clicked');
  return <Child onClick={handleClick} />;
}

function Parent() {
  // GOOD: Same function reference (child doesn't re-render)
  const handleClick = useCallback(() => {
    console.log('clicked');
  }, []);

  return <Child onClick={handleClick} />;
}

// With dependencies
function SearchResults({ query }) {
  const handleSearch = useCallback(() => {
    api.search(query);  // Uses current query
  }, [query]);  // Recreate when query changes

  return <SearchButton onClick={handleSearch} />;
}

// ============================================
// 4. LAZY LOADING - Code Splitting
// ============================================

// BAD: Large bundle loaded upfront
import HeavyComponent from './HeavyComponent';

function App() {
  return <HeavyComponent />;
}

// GOOD: Split into separate bundle
const HeavyComponent = React.lazy(() => import('./HeavyComponent'));

function App() {
  return (
    <Suspense fallback={<Spinner />}>
      <HeavyComponent />
    </Suspense>
  );
}

// Route-based code splitting
const Dashboard = React.lazy(() => import('./pages/Dashboard'));
const Settings = React.lazy(() => import('./pages/Settings'));

function App() {
  return (
    <Suspense fallback={<Spinner />}>
      <Routes>
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </Suspense>
  );
}

// ============================================
// 5. VIRTUAL SCROLLING - Long Lists
// ============================================

import { FixedSizeList } from 'react-window';

// BAD: Renders 10,000 rows (very slow)
function UserList({ users }) {
  return (
    <div>
      {users.map(user => <UserRow key={user.id} user={user} />)}
    </div>
  );
}

// GOOD: Only renders visible rows
function UserList({ users }) {
  return (
    <FixedSizeList
      height={600}
      itemCount={users.length}
      itemSize={50}
      width="100%"
    >
      {({ index, style }) => (
        <div style={style}>
          <UserRow user={users[index]} />
        </div>
      )}
    </FixedSizeList>
  );
}

// ============================================
// 6. DEBOUNCING - Reduce Function Calls
// ============================================

import { debounce } from 'lodash';

function SearchInput() {
  const [query, setQuery] = useState('');

  // BAD: API call on every keystroke
  const handleChange = (e) => {
    setQuery(e.target.value);
    api.search(e.target.value);  // 100 API calls for "hello world"
  };

  return <input onChange={handleChange} />;
}

function SearchInput() {
  const [query, setQuery] = useState('');

  // GOOD: Wait for user to stop typing
  const debouncedSearch = useMemo(
    () => debounce((q) => api.search(q), 300),
    []
  );

  const handleChange = (e) => {
    setQuery(e.target.value);
    debouncedSearch(e.target.value);  // 1 API call after 300ms pause
  };

  return <input value={query} onChange={handleChange} />;
}

// ============================================
// 7. THROTTLING - Limit Execution Rate
// ============================================

import { throttle } from 'lodash';

function ScrollTracker() {
  // BAD: Updates state on every scroll event (60+ times/sec)
  const handleScroll = (e) => {
    setScrollPosition(e.target.scrollTop);
  };

  return <div onScroll={handleScroll}>{/* ... */}</div>;
}

function ScrollTracker() {
  // GOOD: Update at most every 100ms
  const throttledScroll = useMemo(
    () => throttle((position) => setScrollPosition(position), 100),
    []
  );

  const handleScroll = (e) => {
    throttledScroll(e.target.scrollTop);
  };

  return <div onScroll={handleScroll}>{/* ... */}</div>;
}

// ============================================
// 8. AVOID INLINE OBJECTS/ARRAYS
// ============================================

// BAD: New object/array on every render (breaks memoization)
function Parent() {
  return <Child style={{ color: 'red' }} options={['a', 'b']} />;
}

// GOOD: Stable references
const STYLE = { color: 'red' };
const OPTIONS = ['a', 'b'];

function Parent() {
  return <Child style={STYLE} options={OPTIONS} />;
}

// ============================================
// 9. KEY PROP OPTIMIZATION
// ============================================

// BAD: Array index as key (breaks reconciliation on reorder)
users.map((user, index) => <User key={index} user={user} />)

// GOOD: Stable unique ID
users.map(user => <User key={user.id} user={user} />)

// ============================================
// 10. PRODUCTION BUILD
// ============================================

// Development build: Large bundle, slow runtime
// Production build: Minified, optimized

// Vite
npm run build

// Create React App
npm run build

// Check if running in production
if (process.env.NODE_ENV === 'production') {
  // Production-only optimizations
}

// ============================================
// PERFORMANCE MEASUREMENT
// ============================================

// React DevTools Profiler
import { Profiler } from 'react';

function onRenderCallback(id, phase, actualDuration) {
  console.log(`${id} (${phase}) took ${actualDuration}ms`);
}

function App() {
  return (
    <Profiler id="App" onRender={onRenderCallback}>
      <Components />
    </Profiler>
  );
}

// Chrome DevTools Performance tab
// 1. Open DevTools → Performance
// 2. Click Record
// 3. Interact with app
// 4. Stop recording
// 5. Analyze flame graph
