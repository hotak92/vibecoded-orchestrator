# Common Performance Optimization Patterns

## Frontend Optimizations

### 1. Lazy Loading (Code Splitting)

**Before**:
```javascript
import HeavyComponent from './HeavyComponent';
// 500KB bundle loaded upfront
```

**After**:
```javascript
const HeavyComponent = lazy(() => import('./HeavyComponent'));
// Loaded only when needed, reduces initial bundle by 80%
```

**Impact**: Initial load time reduced by 40-60%

---

### 2. Memoization (Prevent Re-renders)

**Before**:
```jsx
function ExpensiveList({ items }) {
  const processedItems = items.map(expensiveTransform); // Runs every render
  return <List items={processedItems} />;
}
```

**After**:
```jsx
function ExpensiveList({ items }) {
  const processedItems = useMemo(
    () => items.map(expensiveTransform),
    [items]  // Only recompute when items change
  );
  return <List items={processedItems} />;
}
```

**Impact**: 70-90% fewer re-renders for expensive computations

---

### 3. Virtual Scrolling (Long Lists)

**Before**:
```jsx
<div>
  {items.map(item => <Row key={item.id} data={item} />)}
  {/* Rendering 10,000 rows = 5s render time */}
</div>
```

**After**:
```jsx
import { FixedSizeList } from 'react-window';

<FixedSizeList height={600} itemCount={10000} itemSize={35}>
  {({ index, style }) => <Row style={style} data={items[index]} />}
</FixedSizeList>
{/* Only renders visible rows = <100ms */}
```

**Impact**: 50-100x faster for lists >100 items

---

### 4. Image Optimization

**Before**:
```html
<img src="photo.jpg" />  <!-- 2MB JPEG -->
```

**After**:
```html
<picture>
  <source srcset="photo-small.webp 480w, photo-large.webp 1080w" type="image/webp">
  <img src="photo.jpg" loading="lazy" />
</picture>
<!-- 200KB WebP with lazy loading -->
```

**Impact**: 90% smaller images, 3x faster page load

---

## Backend Optimizations

### 1. Database Query Optimization

**Before (N+1 Query)**:
```python
users = User.all()  # 1 query
for user in users:
    posts = user.posts()  # N queries (one per user)
# Total: 1 + N queries (N = 100 users → 101 queries)
```

**After (Eager Loading)**:
```python
users = User.with_('posts').all()  # 1 query with JOIN
for user in users:
    posts = user.posts  # No additional query
# Total: 1 query (100x faster)
```

**Impact**: 90-99% reduction in query time

---

### 2. Caching (Redis)

**Before**:
```python
@app.route('/api/popular-posts')
def popular_posts():
    posts = db.query("SELECT ... ORDER BY views DESC LIMIT 10")
    # Database query every request (50ms)
    return posts
```

**After**:
```python
@app.route('/api/popular-posts')
def popular_posts():
    cached = redis.get('popular_posts')
    if cached:
        return cached  # <1ms

    posts = db.query("SELECT ... ORDER BY views DESC LIMIT 10")
    redis.setex('popular_posts', 300, posts)  # Cache 5 min
    return posts
```

**Impact**: 50x faster response time (50ms → 1ms)

---

### 3. Connection Pooling

**Before**:
```python
def query_db(sql):
    conn = psycopg2.connect(DB_URL)  # New connection every query (50ms overhead)
    cursor = conn.cursor()
    cursor.execute(sql)
    conn.close()
```

**After**:
```python
pool = psycopg2.pool.SimpleConnectionPool(10, 20, DB_URL)

def query_db(sql):
    conn = pool.getconn()  # Reuse connection (<1ms)
    cursor = conn.cursor()
    cursor.execute(sql)
    pool.putconn(conn)
```

**Impact**: 90% reduction in connection overhead

---

### 4. Async Processing

**Before (Blocking)**:
```python
@app.route('/api/send-email', methods=['POST'])
def send_email():
    email.send(to, subject, body)  # Blocks for 2s
    return {'status': 'sent'}
```

**After (Background Job)**:
```python
@app.route('/api/send-email', methods=['POST'])
def send_email():
    task_queue.enqueue(send_email_task, to, subject, body)  # <10ms
    return {'status': 'queued'}
```

**Impact**: 200x faster response (2s → 10ms)

---

## AI/ML Optimizations

### 1. Model Quantization

**Before**:
```python
# Load FP16 model (14GB VRAM, 8 tokens/sec)
model = load_model("llama-7b-fp16")
```

**After**:
```python
# Load Q4_K_M quantized (4GB VRAM, 25 tokens/sec)
model = load_model("llama-7b-Q4_K_M.gguf")
```

**Impact**: 70% less VRAM, 3x faster inference

---

### 2. Batch Processing

**Before**:
```python
for prompt in prompts:
    result = model.generate(prompt)  # Process one at a time
# 10 prompts × 2s = 20s total
```

**After**:
```python
results = model.generate_batch(prompts, batch_size=10)
# 10 prompts in 4s (5x faster)
```

**Impact**: 5-10x throughput increase

---

### 3. Context Caching

**Before**:
```python
system_prompt = "You are a helpful assistant..."
user_prompt = "Translate: hello"

response = model.generate(system_prompt + user_prompt)
# System prompt re-processed every request (50 tokens overhead)
```

**After**:
```python
# Cache system prompt embeddings
system_cache = model.cache_context(system_prompt)

response = model.generate_with_cache(system_cache, user_prompt)
# System prompt cached, only process user input
```

**Impact**: 30-50% faster for repeated system prompts

---

### 4. Parallel Inference (Multi-Model)

**Before**:
```python
embedding = embedding_model.encode(text)  # 100ms
response = llm.generate(prompt)  # 2s
# Total: 2.1s sequential
```

**After**:
```python
import asyncio

embedding_task = asyncio.create_task(embedding_model.encode_async(text))
response_task = asyncio.create_task(llm.generate_async(prompt))

embedding, response = await asyncio.gather(embedding_task, response_task)
# Total: 2s parallel (saves 100ms)
```

**Impact**: 5-10% time saved when models independent

---

## Profiling Tools by Domain

**Frontend**:
- Lighthouse (Chrome DevTools)
- WebPageTest
- React DevTools Profiler

**Backend**:
- py-spy (Python profiler)
- Node.js built-in profiler
- Database query analyzers (EXPLAIN)

**AI/ML**:
- NVIDIA nsys (GPU profiling)
- PyTorch Profiler
- VRAM monitoring (nvidia-smi)

---

## Performance Debugging Workflow

1. **Measure baseline**: Quantify current performance (response time, throughput)
2. **Profile**: Identify bottleneck (CPU, memory, database, network)
3. **Optimize hotspot**: Apply pattern from above (caching, indexing, etc.)
4. **Measure improvement**: Validate optimization worked
5. **Repeat**: Move to next bottleneck if target not met
