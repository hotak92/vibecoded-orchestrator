export class MemoryStore {
    elements = new Map();
    maxElements;
    constructor(maxElements = 10_000) {
        this.maxElements = maxElements;
    }
    async get(id) {
        return this.elements.get(id);
    }
    async getAll() {
        return Array.from(this.elements.values());
    }
    async set(id, element) {
        if (this.elements.size >= this.maxElements && !this.elements.has(id)) {
            throw new Error(`Maximum element count (${this.maxElements}) reached. Delete elements before creating new ones.`);
        }
        this.elements.set(id, element);
    }
    async delete(id) {
        return this.elements.delete(id);
    }
    async clear() {
        this.elements.clear();
    }
    async count() {
        return this.elements.size;
    }
    async query(filter) {
        let results = Array.from(this.elements.values());
        if (filter.type) {
            results = results.filter(e => e.type === filter.type);
        }
        if (filter.locked !== undefined) {
            results = results.filter(e => e.locked === filter.locked);
        }
        if (filter.groupId) {
            const gid = filter.groupId;
            results = results.filter(e => e.groupIds?.includes(gid) ?? false);
        }
        return results;
    }
}
