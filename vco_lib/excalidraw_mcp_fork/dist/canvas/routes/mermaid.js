import { Router } from 'express';
import { z } from 'zod';
import { createLogger } from '../../shared/logger.js';
const logger = createLogger('mermaid');
const MermaidBodySchema = z.object({
    mermaidDiagram: z.string().min(1).max(50_000),
    config: z.record(z.string(), z.unknown()).optional(),
}).strict();
export function createMermaidRouter(ws) {
    const router = Router();
    // POST /api/elements/from-mermaid - trigger Mermaid conversion via frontend
    router.post('/from-mermaid', (req, res) => {
        try {
            const result = MermaidBodySchema.safeParse(req.body);
            if (!result.success) {
                res.status(400).json({
                    success: false,
                    error: 'Validation failed',
                    details: result.error.issues,
                });
                return;
            }
            const timestamp = new Date().toISOString();
            ws.broadcast({
                type: 'mermaid_convert',
                mermaidDiagram: result.data.mermaidDiagram,
                config: result.data.config,
                timestamp,
            });
            logger.info({ length: result.data.mermaidDiagram.length }, 'Mermaid conversion broadcast');
            res.json({
                success: true,
                message: 'Mermaid conversion broadcast to connected clients',
                timestamp,
            });
        }
        catch (err) {
            logger.error({ err }, 'Failed to broadcast mermaid conversion');
            res.status(500).json({ success: false, error: 'Internal server error' });
        }
    });
    return router;
}
