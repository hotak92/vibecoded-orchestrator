import type { Request, Response, NextFunction } from 'express';
import type { Config } from '../../shared/config.js';
export declare function createAuthMiddleware(config: Config): (req: Request, res: Response, next: NextFunction) => void;
//# sourceMappingURL=auth.d.ts.map