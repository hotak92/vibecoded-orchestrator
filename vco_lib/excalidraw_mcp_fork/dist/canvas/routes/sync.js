import { Router } from 'express';
import { z } from 'zod';
import { generateId } from '../../shared/id.js';
import { createLogger } from '../../shared/logger.js';
const logger = createLogger('sync');
const SyncBodySchema = z.object({
    elements: z.array(z.record(z.string(), z.unknown())).max(10_000),
}).strict();
export function createSyncRouter(store, ws) {
    const router = Router();
    // POST /api/elements/sync - destructive overwrite sync
    router.post('/sync', async (req, res) => {
        try {
            const result = SyncBodySchema.safeParse(req.body);
            if (!result.success) {
                res.status(400).json({
                    success: false,
                    error: 'Validation failed',
                    details: result.error.issues,
                });
                return;
            }
            const beforeCount = await store.count();
            const timestamp = new Date().toISOString();
            await store.clear();
            const elements = result.data.elements;
            for (const raw of elements) {
                const id = (typeof raw.id === 'string' && raw.id.length <= 64)
                    ? raw.id
                    : generateId();
                const element = {
                    id,
                    type: raw.type ?? 'rectangle',
                    x: raw.x ?? 0,
                    y: raw.y ?? 0,
                    width: raw.width,
                    height: raw.height,
                    createdAt: timestamp,
                    updatedAt: timestamp,
                    version: 1,
                    source: 'sync',
                };
                await store.set(id, element);
            }
            const afterCount = await store.count();
            ws.broadcast({
                type: 'sync_status',
                elementCount: afterCount,
                timestamp,
            });
            logger.info({ beforeCount, afterCount }, 'Canvas synced');
            res.json({
                success: true,
                count: afterCount,
                syncedAt: timestamp,
                beforeCount,
                afterCount,
            });
        }
        catch (err) {
            logger.error({ err }, 'Failed to sync');
            res.status(500).json({ success: false, error: 'Internal server error' });
        }
    });
    return router;
}
