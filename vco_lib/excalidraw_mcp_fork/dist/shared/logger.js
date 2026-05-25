import pino from 'pino';
const isDev = process.env['NODE_ENV'] !== 'production';
const level = process.env['LOG_LEVEL'] ?? 'info';
const transport = isDev
    ? pino.transport({
        target: 'pino-pretty',
        options: {
            destination: 2, // stderr
            colorize: true,
            translateTime: 'SYS:HH:MM:ss.l',
            ignore: 'pid,hostname',
        },
    })
    : pino.destination(2); // stderr in production
const rootLogger = pino({ level }, transport);
export function createLogger(name) {
    return rootLogger.child({ name });
}
export default rootLogger;
