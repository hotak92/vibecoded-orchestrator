import type { CanvasClient } from '../canvas-client.js';
export declare function getResourceTool(args: unknown, client: CanvasClient): Promise<{
    success: boolean;
    resource: string;
    elements: import("../../shared/types.js").ServerElement[];
    count: number;
    scene?: undefined;
    theme?: undefined;
    items?: undefined;
} | {
    success: boolean;
    resource: string;
    scene: {
        theme: string;
        viewport: {
            x: number;
            y: number;
            zoom: number;
        };
    };
    elements?: undefined;
    count?: undefined;
    theme?: undefined;
    items?: undefined;
} | {
    success: boolean;
    resource: string;
    theme: string;
    elements?: undefined;
    count?: undefined;
    scene?: undefined;
    items?: undefined;
} | {
    success: boolean;
    resource: string;
    items: never[];
    elements?: undefined;
    count?: undefined;
    scene?: undefined;
    theme?: undefined;
}>;
//# sourceMappingURL=get-resource.d.ts.map