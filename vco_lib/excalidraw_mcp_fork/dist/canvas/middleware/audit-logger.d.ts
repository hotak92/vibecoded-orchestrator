import type { Request, Response, NextFunction } from 'express';
import type { Config } from '../../shared/config.js';
export declare function createAuditLogger(config: Config): (_req: Request, _res: Response, next: NextFunction) => void;
//# sourceMappingURL=audit-logger.d.ts.map