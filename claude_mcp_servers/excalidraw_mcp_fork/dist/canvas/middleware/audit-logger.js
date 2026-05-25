import { createLogger } from '../../shared/logger.js';
const logger = createLogger('audit');
export function createAuditLogger(config) {
    if (!config.AUDIT_LOG_ENABLED) {
        return (_req, _res, next) => next();
    }
    return (req, res, next) => {
        const start = Date.now();
        res.on('finish', () => {
            const duration = Date.now() - start;
            logger.info({
                method: req.method,
                path: req.path,
                status: res.statusCode,
                duration,
                authenticated: !!req.header('X-API-Key'),
            });
        });
        next();
    };
}
