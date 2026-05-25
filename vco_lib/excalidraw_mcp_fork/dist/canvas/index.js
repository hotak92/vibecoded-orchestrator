import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import express from 'express';
import { loadConfig } from '../shared/config.js';
import { createLogger } from '../shared/logger.js';
import { createSecurityHeaders } from './middleware/security-headers.js';
import { createCorsMiddleware } from './middleware/cors-config.js';
import { createRateLimiter, createStrictRateLimiter } from './middleware/rate-limit.js';
import { createAuditLogger } from './middleware/audit-logger.js';
import { createAuthMiddleware } from './middleware/auth.js';
import { createHealthRouter } from './routes/health.js';
import { createElementsRouter } from './routes/elements.js';
import { createSyncRouter } from './routes/sync.js';
import { createMermaidRouter } from './routes/mermaid.js';
import { createExportRouter } from './routes/export.js';
import { createWebSocketServer } from './ws/handler.js';
import { MemoryStore } from './store/memory-store.js';
import { FileStore } from './store/file-store.js';
const logger = createLogger('canvas');
async function main() {
    const config = loadConfig();
    // Initialize store
    let store;
    if (config.PERSISTENCE_ENABLED) {
        const fileStore = new FileStore(config.PERSISTENCE_DIR, config.MAX_ELEMENTS);
        await fileStore.initialize();
        store = fileStore;
        logger.info({ dir: config.PERSISTENCE_DIR }, 'File persistence enabled');
    }
    else {
        store = new MemoryStore(config.MAX_ELEMENTS);
        logger.info('In-memory storage (no persistence)');
    }
    const app = express();
    // 1. Security headers (first)
    app.use(createSecurityHeaders());
    // 2. Body parsing with size limit
    app.use(express.json({ limit: '512kb' }));
    // 3. CORS (before auth so OPTIONS preflight works)
    app.use(createCorsMiddleware(config));
    // 4. Rate limiting (before auth to prevent brute-force)
    const standardLimiter = createRateLimiter(config);
    const strictLimiter = createStrictRateLimiter(config);
    app.use(standardLimiter);
    // 5. Audit logging
    app.use(createAuditLogger(config));
    // 6. Health check route (no auth required)
    app.use(createHealthRouter());
    // 7. Serve frontend static files (no auth - frontend handles its own auth for WS/API)
    const __dirname = path.dirname(fileURLToPath(import.meta.url));
    const frontendDir = path.resolve(__dirname, 'frontend');
    app.use(express.static(frontendDir));
    // 8. Authentication (only applies to /api/* routes below)
    app.use('/api', createAuthMiddleware(config));
    // Create HTTP server for both Express and WebSocket
    const server = http.createServer(app);
    // 9. WebSocket with auth
    const wsContext = createWebSocketServer(server, config, store);
    // 10. API routes
    const elementsRouter = createElementsRouter(store, wsContext);
    app.use('/api/elements', elementsRouter);
    const syncRouter = createSyncRouter(store, wsContext);
    app.use('/api/elements', strictLimiter, syncRouter);
    const mermaidRouter = createMermaidRouter(wsContext);
    app.use('/api/elements', strictLimiter, mermaidRouter);
    const exportRouter = createExportRouter(store);
    app.use('/api/export', strictLimiter, exportRouter);
    // 11. Serve frontend index for all non-API routes (SPA fallback)
    app.get('*', (_req, res) => {
        res.sendFile(path.join(frontendDir, 'index.html'));
    });
    // Start server
    const { CANVAS_HOST: host, CANVAS_PORT: port } = config;
    server.listen(port, host, () => {
        logger.info({ host, port }, `Canvas server running at http://${host}:${port}`);
        logger.info('API key required for all endpoints (except /health)');
        if (config.EXCALIDRAW_API_KEY.length === 64) {
            // Likely auto-generated - show it once
            logger.info({ key: config.EXCALIDRAW_API_KEY }, 'Auto-generated API key (set EXCALIDRAW_API_KEY env var to use your own)');
        }
    });
}
main().catch(err => {
    logger.fatal({ err }, 'Failed to start canvas server');
    process.exit(1);
});
