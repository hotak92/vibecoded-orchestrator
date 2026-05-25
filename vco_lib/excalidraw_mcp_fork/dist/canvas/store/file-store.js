import fs from 'node:fs/promises';
import path from 'node:path';
import { MemoryStore } from './memory-store.js';
import { createLogger } from '../../shared/logger.js';
const logger = createLogger('file-store');
export class FileStore {
    memory;
    filePath;
    saveTimer = null;
    DEBOUNCE_MS = 1000;
    constructor(persistenceDir, maxElements = 10_000) {
        this.memory = new MemoryStore(maxElements);
        this.filePath = path.resolve(persistenceDir, 'canvas-state.json');
    }
    async initialize() {
        try {
            const dir = path.dirname(this.filePath);
            await fs.mkdir(dir, { recursive: true });
            const data = await fs.readFile(this.filePath, 'utf8');
            const parsed = JSON.parse(data);
            if (Array.isArray(parsed.elements)) {
                let loaded = 0;
                for (const el of parsed.elements) {
                    if (el.id && typeof el.id === 'string') {
                        await this.memory.set(el.id, el);
                        loaded++;
                    }
                }
                logger.info({ loaded, path: this.filePath }, 'Loaded elements from disk');
            }
        }
        catch (err) {
            if (err.code === 'ENOENT') {
                logger.info({ path: this.filePath }, 'No persistence file found, starting fresh');
                return;
            }
            throw err;
        }
    }
    async get(id) {
        return this.memory.get(id);
    }
    async getAll() {
        return this.memory.getAll();
    }
    async count() {
        return this.memory.count();
    }
    async query(filter) {
        return this.memory.query(filter);
    }
    async set(id, element) {
        await this.memory.set(id, element);
        this.scheduleSave();
    }
    async delete(id) {
        const result = await this.memory.delete(id);
        if (result)
            this.scheduleSave();
        return result;
    }
    async clear() {
        await this.memory.clear();
        this.scheduleSave();
    }
    scheduleSave() {
        if (this.saveTimer) {
            clearTimeout(this.saveTimer);
        }
        this.saveTimer = setTimeout(() => {
            this.saveToDisk().catch(err => {
                logger.error({ err }, 'Failed to save to disk');
            });
        }, this.DEBOUNCE_MS);
    }
    async saveToDisk() {
        const elements = await this.memory.getAll();
        const data = JSON.stringify({
            version: 1,
            savedAt: new Date().toISOString(),
            elements,
        }, null, 2);
        const tmpPath = this.filePath + '.tmp';
        await fs.writeFile(tmpPath, data, 'utf8');
        await fs.rename(tmpPath, this.filePath);
        logger.debug({ count: elements.length }, 'Saved to disk');
    }
}
