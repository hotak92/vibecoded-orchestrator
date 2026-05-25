import rateLimit from 'express-rate-limit';
export function createRateLimiter(config) {
    return rateLimit({
        windowMs: config.RATE_LIMIT_WINDOW_MS,
        max: config.RATE_LIMIT_MAX_REQUESTS,
        standardHeaders: true,
        legacyHeaders: false,
        message: { success: false, error: 'Too many requests. Try again later.' },
        keyGenerator: (req) => req.header('X-API-Key') ?? req.ip ?? 'unknown',
    });
}
// Stricter limiter for destructive operations
export function createStrictRateLimiter(config) {
    return rateLimit({
        windowMs: config.RATE_LIMIT_WINDOW_MS,
        max: Math.ceil(config.RATE_LIMIT_MAX_REQUESTS / 5),
        standardHeaders: true,
        legacyHeaders: false,
        message: { success: false, error: 'Rate limit exceeded for destructive operations.' },
        keyGenerator: (req) => req.header('X-API-Key') ?? req.ip ?? 'unknown',
    });
}
