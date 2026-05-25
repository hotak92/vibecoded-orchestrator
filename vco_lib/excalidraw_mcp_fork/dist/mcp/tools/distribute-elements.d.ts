import type { CanvasClient } from '../canvas-client.js';
export declare function distributeElementsTool(args: unknown, client: CanvasClient): Promise<{
    success: boolean;
    distributed: boolean;
    direction: "horizontal" | "vertical";
    elementIds: string[];
}>;
//# sourceMappingURL=distribute-elements.d.ts.map