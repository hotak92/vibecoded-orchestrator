import type { ServerElement } from '../../shared/types.js';
import { MemoryStore } from '../../canvas/store/memory-store.js';
/**
 * In-process element store for standalone mode (no canvas server needed).
 * Extends MemoryStore with replaceAll for sync operations and
 * checkpoint/restore for undo support.
 */
export declare class StandaloneStore extends MemoryStore {
    private snapshot;
    /**
     * Replace all elements atomically (used by sync operations).
     */
    replaceAll(elements: ServerElement[]): Promise<void>;
    /**
     * Save a snapshot of the current state that can be restored later.
     */
    checkpoint(): Promise<number>;
    /**
     * Restore the last saved checkpoint. Returns false if no checkpoint exists.
     */
    restore(): Promise<boolean>;
    hasCheckpoint(): boolean;
}
//# sourceMappingURL=standalone-store.d.ts.map