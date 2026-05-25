import crypto from 'node:crypto';
import { z } from 'zod';
const configSchema = z.object({
    CANVAS_HOST: z
        .string()
        .default('127.0.0.1'),
    CANVAS_PORT: z
        .coerce.number()
        .int()
        .min(1)
        .max(65535)
        .default(3000),
    EXCALIDRAW_API_KEY: z
        .string()
        .min(32)
        .default(crypto.randomBytes(32).toString('hex')),
    CORS_ALLOWED_ORIGINS: z
        .string()
        .default('http://localhost:3000,http://127.0.0.1:3000'),
    RATE_LIMIT_WINDOW_MS: z
        .coerce.number()
        .int()
        .positive()
        .default(60000),
    RATE_LIMIT_MAX_REQUESTS: z
        .coerce.number()
        .int()
        .positive()
        .default(100),
    PERSISTENCE_ENABLED: z
        .coerce.boolean()
        .default(false),
    PERSISTENCE_DIR: z
        .string()
        .default('./data'),
    CANVAS_SERVER_URL: z
        .string()
        .url()
        .default('http://127.0.0.1:3000'),
    LOG_LEVEL: z
        .enum(['debug', 'info', 'warn', 'error'])
        .default('info'),
    AUDIT_LOG_ENABLED: z
        .coerce.boolean()
        .default(true),
    MAX_ELEMENTS: z
        .coerce.number()
        .int()
        .positive()
        .default(10000),
    MAX_BATCH_SIZE: z
        .coerce.number()
        .int()
        .positive()
        .default(100),
    STANDALONE_MODE: z
        .coerce.boolean()
        .default(true),
    STREAMABLE_HTTP_PORT: z
        .coerce.number()
        .int()
        .min(1)
        .max(65535)
        .optional(),
});
export function loadConfig() {
    return configSchema.parse(process.env);
}
