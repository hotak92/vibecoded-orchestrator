import type { CanvasClient } from '../canvas-client.js';
export declare function alignElementsTool(args: unknown, client: CanvasClient): Promise<{
    success: boolean;
    aligned: boolean;
    alignment: "left" | "center" | "right" | "top" | "middle" | "bottom";
    elementIds: string[];
}>;
//# sourceMappingURL=align-elements.d.ts.map