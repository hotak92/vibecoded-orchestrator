import cors from 'cors';
import type { Config } from '../../shared/config.js';
export declare function createCorsMiddleware(config: Config): (req: cors.CorsRequest, res: {
    statusCode?: number | undefined;
    setHeader(key: string, value: string): any;
    end(): any;
}, next: (err?: any) => any) => void;
//# sourceMappingURL=cors-config.d.ts.map