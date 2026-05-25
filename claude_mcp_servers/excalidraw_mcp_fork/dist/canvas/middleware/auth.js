import crypto from 'node:crypto';
export function createAuthMiddleware(config) {
    const expectedKey = config.EXCALIDRAW_API_KEY;
    return (req, res, next) => {
        // Allow health check without auth
        if (req.path === '/health') {
            return next();
        }
        const providedKey = req.header('X-API-Key');
        if (!providedKey) {
            res.status(401).json({
                success: false,
                error: 'Missing API key. Provide X-API-Key header.',
            });
            return;
        }
        // Constant-time comparison to prevent timing attacks
        const expected = Buffer.from(expectedKey, 'utf8');
        const provided = Buffer.from(providedKey, 'utf8');
        if (expected.length !== provided.length ||
            !crypto.timingSafeEqual(expected, provided)) {
            res.status(403).json({
                success: false,
                error: 'Invalid API key.',
            });
            return;
        }
        next();
    };
}
