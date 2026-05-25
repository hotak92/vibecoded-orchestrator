import type { ExcalidrawElementType } from './constants.js';
export interface Point {
    x: number;
    y: number;
}
export interface ServerElement {
    id: string;
    type: ExcalidrawElementType;
    x: number;
    y: number;
    width?: number;
    height?: number;
    points?: Point[];
    backgroundColor?: string;
    strokeColor?: string;
    strokeWidth?: number;
    roughness?: number;
    opacity?: number;
    text?: string;
    fontSize?: number;
    fontFamily?: number;
    groupIds?: string[];
    locked?: boolean;
    angle?: number;
    fillStyle?: string;
    strokeStyle?: string;
    boundElements?: Array<{
        id: string;
        type: string;
    }>;
    containerId?: string | null;
    createdAt: string;
    updatedAt: string;
    version: number;
    source?: string;
}
export interface ApiResponse<T = unknown> {
    success: boolean;
    data?: T;
    error?: string;
    message?: string;
}
export interface ElementsResponse {
    success: boolean;
    elements: ServerElement[];
    count: number;
}
export interface SyncResponse {
    success: boolean;
    count: number;
    syncedAt: string;
    beforeCount: number;
    afterCount: number;
}
export interface ElementFilter {
    type?: ExcalidrawElementType;
    locked?: boolean;
    groupId?: string;
}
export interface WsInitialElements {
    type: 'initial_elements';
    elements: ServerElement[];
}
export interface WsElementCreated {
    type: 'element_created';
    element: ServerElement;
}
export interface WsElementUpdated {
    type: 'element_updated';
    element: ServerElement;
}
export interface WsElementDeleted {
    type: 'element_deleted';
    elementId: string;
}
export interface WsBatchCreated {
    type: 'elements_batch_created';
    elements: ServerElement[];
}
export interface WsSyncStatus {
    type: 'sync_status';
    elementCount: number;
    timestamp: string;
}
export interface WsMermaidConvert {
    type: 'mermaid_convert';
    mermaidDiagram: string;
    config?: Record<string, unknown>;
    timestamp: string;
}
export interface WsError {
    type: 'error';
    error: string;
}
export type ServerWsMessage = WsInitialElements | WsElementCreated | WsElementUpdated | WsElementDeleted | WsBatchCreated | WsSyncStatus | WsMermaidConvert | WsError;
//# sourceMappingURL=types.d.ts.map