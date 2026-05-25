import type { Config } from '../shared/config.js';
import type { ServerElement, SyncResponse } from '../shared/types.js';
export declare class CanvasClient {
    private baseUrl;
    private apiKey;
    constructor(config: Config);
    private headers;
    private safePath;
    createElement(data: Record<string, unknown>): Promise<ServerElement>;
    getElement(id: string): Promise<ServerElement | null>;
    getAllElements(): Promise<ServerElement[]>;
    updateElement(id: string, data: Record<string, unknown>): Promise<ServerElement>;
    deleteElement(id: string): Promise<boolean>;
    batchCreate(elements: Record<string, unknown>[]): Promise<ServerElement[]>;
    searchElements(filter: Record<string, string>): Promise<ServerElement[]>;
    sync(elements: Record<string, unknown>[]): Promise<SyncResponse>;
    convertMermaid(mermaidDiagram: string, config?: Record<string, unknown>): Promise<void>;
    exportScene(format: 'png' | 'svg', options?: {
        elementIds?: string[];
        background?: string;
        padding?: number;
    }): Promise<{
        data: string | Record<string, unknown>;
        contentType: string;
    }>;
    healthCheck(): Promise<boolean>;
}
//# sourceMappingURL=canvas-client.d.ts.map