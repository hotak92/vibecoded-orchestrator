import type { ServerElement, ElementFilter } from '../../shared/types.js';
import type { ElementStore } from './store.js';
export declare class FileStore implements ElementStore {
    private memory;
    private filePath;
    private saveTimer;
    private readonly DEBOUNCE_MS;
    constructor(persistenceDir: string, maxElements?: number);
    initialize(): Promise<void>;
    get(id: string): Promise<ServerElement | undefined>;
    getAll(): Promise<ServerElement[]>;
    count(): Promise<number>;
    query(filter: ElementFilter): Promise<ServerElement[]>;
    set(id: string, element: ServerElement): Promise<void>;
    delete(id: string): Promise<boolean>;
    clear(): Promise<void>;
    private scheduleSave;
    private saveToDisk;
}
//# sourceMappingURL=file-store.d.ts.map