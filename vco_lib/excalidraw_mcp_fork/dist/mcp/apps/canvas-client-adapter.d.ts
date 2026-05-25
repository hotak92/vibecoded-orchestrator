import type { ServerElement, SyncResponse } from '../../shared/types.js';
import type { StandaloneStore } from './standalone-store.js';
/**
 * Adapts StandaloneStore to match the CanvasClient interface so the
 * existing 14 MCP tools can work identically in standalone mode
 * without any code changes.
 */
export declare class CanvasClientAdapter {
    private store;
    constructor(store: StandaloneStore);
    createElement(data: Record<string, unknown>): Promise<ServerElement>;
    getElement(id: string): Promise<ServerElement | null>;
    getAllElements(): Promise<ServerElement[]>;
    updateElement(id: string, data: Record<string, unknown>): Promise<ServerElement>;
    deleteElement(id: string): Promise<boolean>;
    batchCreate(elements: Record<string, unknown>[]): Promise<ServerElement[]>;
    searchElements(filter: Record<string, string>): Promise<ServerElement[]>;
    sync(elements: Record<string, unknown>[]): Promise<SyncResponse>;
    convertMermaid(_mermaidDiagram: string, _config?: Record<string, unknown>): Promise<void>;
    exportScene(format: 'png' | 'svg', _options?: {
        elementIds?: string[];
        background?: string;
        padding?: number;
    }): Promise<{
        data: string | Record<string, unknown>;
        contentType: string;
    }>;
    healthCheck(): Promise<boolean>;
}
//# sourceMappingURL=canvas-client-adapter.d.ts.map