import { Router } from 'express';
import { z } from 'zod';
import { generateId } from '../../shared/id.js';
import { createLogger } from '../../shared/logger.js';
const logger = createLogger('elements');
const MAX_ID = 64;
const MAX_COLOR = 32;
const MAX_TEXT = 10_000;
const MAX_BATCH = 100;
const ELEMENT_TYPES = [
    'rectangle', 'ellipse', 'diamond', 'arrow',
    'text', 'line', 'freedraw',
];
const CoordSchema = z.number().min(-1_000_000).max(1_000_000).finite();
const DimSchema = z.number().min(0).max(100_000).finite().optional();
const ColorSchema = z.string().max(MAX_COLOR).optional();
const CreateBodySchema = z.object({
    type: z.enum(ELEMENT_TYPES),
    x: CoordSchema,
    y: CoordSchema,
    width: DimSchema,
    height: DimSchema,
    backgroundColor: ColorSchema,
    strokeColor: ColorSchema,
    strokeWidth: z.number().min(0).max(100).finite().optional(),
    roughness: z.number().min(0).max(3).finite().optional(),
    opacity: z.number().min(0).max(100).finite().optional(),
    text: z.string().max(MAX_TEXT).optional(),
    fontSize: z.number().min(1).max(1000).finite().optional(),
    fontFamily: z.number().int().min(1).max(4).optional(),
    groupIds: z.array(z.string().max(64)).max(50).optional(),
    locked: z.boolean().optional(),
    angle: z.number().min(-360).max(360).finite().optional(),
    points: z.array(z.object({
        x: CoordSchema,
        y: CoordSchema,
    }).strict()).max(10_000).optional(),
}).strict();
const UpdateBodySchema = CreateBodySchema.partial().strict();
const BatchBodySchema = z.object({
    elements: z.array(CreateBodySchema).min(1).max(MAX_BATCH),
}).strict();
export function createElementsRouter(store, ws) {
    const router = Router();
    // GET /api/elements - list all
    router.get('/', async (_req, res) => {
        try {
            const elements = await store.getAll();
            res.json({ success: true, elements, count: elements.length });
        }
        catch (err) {
            logger.error({ err }, 'Failed to list elements');
            res.status(500).json({ success: false, error: 'Internal server error' });
        }
    });
    // GET /api/elements/search - query with filters
    router.get('/search', async (req, res) => {
        try {
            const type = typeof req.query.type === 'string' ? req.query.type : undefined;
            const locked = req.query.locked === 'true' ? true : req.query.locked === 'false' ? false : undefined;
            const groupId = typeof req.query.groupId === 'string' ? req.query.groupId : undefined;
            const elements = await store.query({ type: type, locked, groupId });
            res.json({ success: true, elements, count: elements.length });
        }
        catch (err) {
            logger.error({ err }, 'Failed to search elements');
            res.status(500).json({ success: false, error: 'Internal server error' });
        }
    });
    // GET /api/elements/:id - get by id
    router.get('/:id', async (req, res) => {
        try {
            const id = req.params.id;
            if (!id || id.length > MAX_ID) {
                res.status(400).json({ success: false, error: 'Invalid element ID' });
                return;
            }
            const element = await store.get(id);
            if (!element) {
                res.status(404).json({ success: false, error: `Element ${id} not found` });
                return;
            }
            res.json({ success: true, element });
        }
        catch (err) {
            logger.error({ err }, 'Failed to get element');
            res.status(500).json({ success: false, error: 'Internal server error' });
        }
    });
    // POST /api/elements - create
    router.post('/', async (req, res) => {
        try {
            const result = CreateBodySchema.safeParse(req.body);
            if (!result.success) {
                res.status(400).json({
                    success: false,
                    error: 'Validation failed',
                    details: result.error.issues,
                });
                return;
            }
            const now = new Date().toISOString();
            const id = generateId();
            const element = {
                ...result.data,
                id,
                createdAt: now,
                updatedAt: now,
                version: 1,
                source: 'api',
            };
            await store.set(id, element);
            ws.broadcast({
                type: 'element_created',
                element: element,
            });
            res.status(201).json({ success: true, element });
        }
        catch (err) {
            const message = err instanceof Error ? err.message : 'Internal server error';
            logger.error({ err }, 'Failed to create element');
            res.status(message.includes('Maximum element count') ? 409 : 500).json({
                success: false,
                error: message,
            });
        }
    });
    // PUT /api/elements/:id - update
    router.put('/:id', async (req, res) => {
        try {
            const id = req.params.id;
            if (!id || id.length > MAX_ID) {
                res.status(400).json({ success: false, error: 'Invalid element ID' });
                return;
            }
            const existing = await store.get(id);
            if (!existing) {
                res.status(404).json({ success: false, error: `Element ${id} not found` });
                return;
            }
            const result = UpdateBodySchema.safeParse(req.body);
            if (!result.success) {
                res.status(400).json({
                    success: false,
                    error: 'Validation failed',
                    details: result.error.issues,
                });
                return;
            }
            const updated = {
                ...existing,
                ...result.data,
                id,
                updatedAt: new Date().toISOString(),
                version: existing.version + 1,
            };
            await store.set(id, updated);
            ws.broadcast({
                type: 'element_updated',
                element: updated,
            });
            res.json({ success: true, element: updated });
        }
        catch (err) {
            logger.error({ err }, 'Failed to update element');
            res.status(500).json({ success: false, error: 'Internal server error' });
        }
    });
    // DELETE /api/elements/:id - delete
    router.delete('/:id', async (req, res) => {
        try {
            const id = req.params.id;
            if (!id || id.length > MAX_ID) {
                res.status(400).json({ success: false, error: 'Invalid element ID' });
                return;
            }
            const deleted = await store.delete(id);
            if (!deleted) {
                res.status(404).json({ success: false, error: `Element ${id} not found` });
                return;
            }
            ws.broadcast({ type: 'element_deleted', elementId: id });
            res.json({ success: true, message: `Element ${id} deleted` });
        }
        catch (err) {
            logger.error({ err }, 'Failed to delete element');
            res.status(500).json({ success: false, error: 'Internal server error' });
        }
    });
    // POST /api/elements/batch - batch create
    router.post('/batch', async (req, res) => {
        try {
            const result = BatchBodySchema.safeParse(req.body);
            if (!result.success) {
                res.status(400).json({
                    success: false,
                    error: 'Validation failed',
                    details: result.error.issues,
                });
                return;
            }
            const now = new Date().toISOString();
            const created = [];
            for (const spec of result.data.elements) {
                const id = generateId();
                const element = {
                    ...spec,
                    id,
                    createdAt: now,
                    updatedAt: now,
                    version: 1,
                    source: 'api_batch',
                };
                await store.set(id, element);
                created.push(element);
            }
            ws.broadcast({
                type: 'elements_batch_created',
                elements: created,
            });
            res.status(201).json({ success: true, elements: created, count: created.length });
        }
        catch (err) {
            const message = err instanceof Error ? err.message : 'Internal server error';
            logger.error({ err }, 'Failed to batch create');
            res.status(message.includes('Maximum element count') ? 409 : 500).json({
                success: false,
                error: message,
            });
        }
    });
    return router;
}
