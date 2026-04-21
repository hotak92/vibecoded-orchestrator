# Performance Optimization Checklists

## Frontend Performance Checklist

**Bundle Size**:
- [ ] Total bundle < 200KB gzipped
- [ ] Code splitting for routes (lazy loading)
- [ ] Tree shaking enabled (remove unused code)
- [ ] Minification in production
- [ ] Dynamic imports for large libraries

**Loading Performance**:
- [ ] First Contentful Paint (FCP) < 1.5s
- [ ] Time to Interactive (TTI) < 3s
- [ ] Largest Contentful Paint (LCP) < 2.5s
- [ ] Cumulative Layout Shift (CLS) = 0
- [ ] No render-blocking resources

**Images & Media**:
- [ ] Images lazy-loaded (loading="lazy")
- [ ] Images optimized (WebP, AVIF formats)
- [ ] Responsive images (srcset for different sizes)
- [ ] CDN for static assets
- [ ] Video poster images for thumbnails

**Rendering Optimization**:
- [ ] Expensive computations memoized (useMemo)
- [ ] Function references stable (useCallback)
- [ ] Components memoized (React.memo)
- [ ] Virtual scrolling for long lists
- [ ] Debounce/throttle frequent events

**Caching**:
- [ ] Service worker for offline support
- [ ] Cache-Control headers set
- [ ] Static assets versioned (cache busting)
- [ ] API responses cached (when appropriate)

---

## Backend Performance Checklist

**Database Queries**:
- [ ] All queries use indexes
- [ ] No N+1 queries (use eager loading)
- [ ] Query result pagination (limit, offset)
- [ ] Connection pooling configured
- [ ] Slow query logging enabled

**API Performance**:
- [ ] Response caching (Redis, in-memory)
- [ ] Rate limiting to prevent abuse
- [ ] Compression enabled (Gzip, Brotli)
- [ ] Response time < 200ms (p95)
- [ ] API responses paginated

**Async Processing**:
- [ ] Non-blocking I/O for file/network operations
- [ ] Background jobs for heavy processing
- [ ] Task queues for asynchronous work
- [ ] Webhooks instead of polling

**Caching**:
- [ ] Redis/Memcached for session data
- [ ] Application-level caching (in-memory)
- [ ] CDN for static content
- [ ] Cache invalidation strategy

**Scalability**:
- [ ] Horizontal scaling possible (stateless)
- [ ] Load balancer configured
- [ ] Database read replicas for queries
- [ ] Auto-scaling rules defined

---

## AI/ML Performance Checklist

**Model Optimization**:
- [ ] Model quantized (Q4_K_M minimum for inference)
- [ ] Model size appropriate for VRAM
- [ ] Batch size optimized (maximize without OOM)
- [ ] Context length within model limits
- [ ] Model loaded once (not per request)

**Inference Performance**:
- [ ] GPU utilization >80% (not bottlenecked)
- [ ] Preprocessing cached (tokenization, embedding)
- [ ] Parallel inference where possible
- [ ] Streaming responses for better UX
- [ ] Fallback for OOM errors

**VRAM Management**:
- [ ] Models offloaded when idle (if multi-model)
- [ ] Context trimming for long conversations
- [ ] KV cache size limited
- [ ] 20% VRAM headroom maintained

**Quality vs Speed Tradeoffs**:
- [ ] Smaller models for simple tasks
- [ ] Higher quantization acceptable? (Q4 vs Q8)
- [ ] Context truncation acceptable?
- [ ] Beam search width optimized

---

## Performance Testing

**Load Testing**:
- [ ] Test with realistic traffic (users, requests/sec)
- [ ] Test with peak load (2-3x normal)
- [ ] Identify bottlenecks (CPU, memory, database, network)
- [ ] Measure p50, p95, p99 latencies

**Profiling**:
- [ ] CPU profiling (identify hot spots)
- [ ] Memory profiling (identify leaks)
- [ ] Database query profiling (slow queries)
- [ ] Network profiling (latency, throughput)

**Monitoring**:
- [ ] Response time tracking
- [ ] Error rate monitoring
- [ ] Resource usage (CPU, memory, disk)
- [ ] Alerts for performance degradation

---

## Common Performance Targets

| Metric | Target | Good | Excellent |
|--------|--------|------|-----------|
| Frontend FCP | <1.5s | <1s | <0.5s |
| Frontend TTI | <3s | <2s | <1s |
| API Response (p95) | <200ms | <100ms | <50ms |
| Database Query | <50ms | <20ms | <10ms |
| AI Inference (7B) | <2s | <1s | <500ms |
| Page Load (full) | <3s | <2s | <1s |
