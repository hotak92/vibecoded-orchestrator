import type { ServerElement, ElementFilter } from '../../shared/types.js';
export interface ElementStore {
    get(id: string): Promise<ServerElement | undefined>;
    getAll(): Promise<ServerElement[]>;
    set(id: string, element: ServerElement): Promise<void>;
    delete(id: string): Promise<boolean>;
    clear(): Promise<void>;
    count(): Promise<number>;
    query(filter: ElementFilter): Promise<ServerElement[]>;
}
//# sourceMappingURL=store.d.ts.map