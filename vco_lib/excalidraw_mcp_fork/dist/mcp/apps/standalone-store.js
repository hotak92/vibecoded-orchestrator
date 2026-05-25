import { MemoryStore } from '../../canvas/store/memory-store.js';
/**
 * In-process element store for standalone mode (no canvas server needed).
 * Extends MemoryStore with replaceAll for sync operations and
 * checkpoint/restore for undo support.
 */
export class StandaloneStore extends MemoryStore {
    snapshot = null;
    /**
     * Replace all elements atomically (used by sync operations).
     */
    async replaceAll(elements) {
        await this.clear();
        for (const el of elements) {
            await this.set(el.id, el);
        }
    }
    /**
     * Save a snapshot of the current state that can be restored later.
     */
    async checkpoint() {
        const all = await this.getAll();
        this.snapshot = new Map(all.map(el => [el.id, structuredClone(el)]));
        return all.length;
    }
    /**
     * Restore the last saved checkpoint. Returns false if no checkpoint exists.
     */
    async restore() {
        if (!this.snapshot)
            return false;
        await this.clear();
        for (const [id, el] of this.snapshot) {
            await this.set(id, el);
        }
        return true;
    }
    hasCheckpoint() {
        return this.snapshot !== null;
    }
}
