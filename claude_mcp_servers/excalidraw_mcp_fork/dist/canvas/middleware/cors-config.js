import cors from 'cors';
export function createCorsMiddleware(config) {
    const allowedOrigins = config.CORS_ALLOWED_ORIGINS
        .split(',')
        .map(o => o.trim())
        .filter(Boolean);
    return cors({
        origin: (origin, callback) => {
            if (!origin)
                return callback(null, true);
            if (allowedOrigins.includes(origin))
                return callback(null, true);
            return callback(new Error(`Origin ${origin} not allowed by CORS`));
        },
        methods: ['GET', 'POST', 'PUT', 'DELETE'],
        allowedHeaders: ['Content-Type', 'X-API-Key'],
        credentials: false,
        maxAge: 600,
    });
}
