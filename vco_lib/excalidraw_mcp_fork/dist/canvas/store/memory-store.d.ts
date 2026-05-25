import type { ServerElement, ElementFilter } from '../../shared/types.js';
import type { ElementStore } from './store.js';
export declare class MemoryStore implements ElementStore {
    private elements;
    private maxElements;
    constructor(maxElements?: number);
    get(id: string): Promise<ServerElement | undefined>;
    getAll(): Promise<ServerElement[]>;
    set(id: string, element: ServerElement): Promise<void>;
    delete(id: string): Promise<boolean>;
    clear(): Promise<void>;
    count(): Promise<number>;
    query(filter: ElementFilter): Promise<ServerElement[]>;
}
//# sourceMappingURL=memory-store.d.ts.map