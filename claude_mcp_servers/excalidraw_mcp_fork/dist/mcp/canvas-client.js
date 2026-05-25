import { createLogger } from '../shared/logger.js';
const logger = createLogger('canvas-client');
export class CanvasClient {
    baseUrl;
    apiKey;
    constructor(config) {
        this.baseUrl = config.CANVAS_SERVER_URL.replace(/\/$/, '');
        this.apiKey = config.EXCALIDRAW_API_KEY;
    }
    headers() {
        return {
            'Content-Type': 'application/json',
            'X-API-Key': this.apiKey,
        };
    }
    safePath(id) {
        return encodeURIComponent(id);
    }
    async createElement(data) {
        const res = await fetch(`${this.baseUrl}/api/elements`, {
            method: 'POST',
            headers: this.headers(),
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error ?? `Canvas error: ${res.status}`);
        }
        const body = await res.json();
        return body.element ?? body.data;
    }
    async getElement(id) {
        const res = await fetch(`${this.baseUrl}/api/elements/${this.safePath(id)}`, { headers: this.headers() });
        if (res.status === 404)
            return null;
        if (!res.ok)
            throw new Error(`Canvas error: ${res.status}`);
        const body = await res.json();
        return body.element ?? null;
    }
    async getAllElements() {
        const res = await fetch(`${this.baseUrl}/api/elements`, {
            headers: this.headers(),
        });
        if (!res.ok)
            throw new Error(`Canvas error: ${res.status}`);
        const body = await res.json();
        return body.elements;
    }
    async updateElement(id, data) {
        const res = await fetch(`${this.baseUrl}/api/elements/${this.safePath(id)}`, {
            method: 'PUT',
            headers: this.headers(),
            body: JSON.stringify(data),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error ?? `Canvas error: ${res.status}`);
        }
        const body = await res.json();
        return body.element;
    }
    async deleteElement(id) {
        const res = await fetch(`${this.baseUrl}/api/elements/${this.safePath(id)}`, { method: 'DELETE', headers: this.headers() });
        if (res.status === 404)
            return false;
        if (!res.ok)
            throw new Error(`Canvas error: ${res.status}`);
        return true;
    }
    async batchCreate(elements) {
        const res = await fetch(`${this.baseUrl}/api/elements/batch`, {
            method: 'POST',
            headers: this.headers(),
            body: JSON.stringify({ elements }),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error ?? `Canvas error: ${res.status}`);
        }
        const body = await res.json();
        return body.elements ?? [];
    }
    async searchElements(filter) {
        const params = new URLSearchParams(filter);
        const res = await fetch(`${this.baseUrl}/api/elements/search?${params.toString()}`, { headers: this.headers() });
        if (!res.ok)
            throw new Error(`Canvas error: ${res.status}`);
        const body = await res.json();
        return body.elements;
    }
    async sync(elements) {
        const res = await fetch(`${this.baseUrl}/api/elements/sync`, {
            method: 'POST',
            headers: this.headers(),
            body: JSON.stringify({ elements }),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error ?? `Canvas error: ${res.status}`);
        }
        return (await res.json());
    }
    async convertMermaid(mermaidDiagram, config) {
        const res = await fetch(`${this.baseUrl}/api/elements/from-mermaid`, {
            method: 'POST',
            headers: this.headers(),
            body: JSON.stringify({ mermaidDiagram, config }),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error ?? `Canvas error: ${res.status}`);
        }
    }
    async exportScene(format, options) {
        const res = await fetch(`${this.baseUrl}/api/export`, {
            method: 'POST',
            headers: this.headers(),
            body: JSON.stringify({ format, ...options }),
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error ?? `Canvas error: ${res.status}`);
        }
        if (format === 'svg') {
            return { data: await res.text(), contentType: 'image/svg+xml' };
        }
        return { data: await res.json(), contentType: 'application/json' };
    }
    async healthCheck() {
        try {
            const res = await fetch(`${this.baseUrl}/health`);
            return res.ok;
        }
        catch {
            logger.warn('Canvas server health check failed');
            return false;
        }
    }
}
