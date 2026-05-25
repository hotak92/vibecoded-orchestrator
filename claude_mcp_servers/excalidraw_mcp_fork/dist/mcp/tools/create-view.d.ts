import { z } from 'zod';
export declare const CREATE_VIEW_SCHEMA: {
    elements: z.ZodArray<z.ZodObject<{
        type: z.ZodEnum<["rectangle", "ellipse", "diamond", "arrow", "text", "line", "freedraw"]>;
        x: z.ZodNumber;
        y: z.ZodNumber;
        width: z.ZodOptional<z.ZodNumber>;
        height: z.ZodOptional<z.ZodNumber>;
        points: z.ZodOptional<z.ZodArray<z.ZodObject<{
            x: z.ZodNumber;
            y: z.ZodNumber;
        }, "strip", z.ZodTypeAny, {
            x: number;
            y: number;
        }, {
            x: number;
            y: number;
        }>, "many">>;
        backgroundColor: z.ZodOptional<z.ZodString>;
        strokeColor: z.ZodOptional<z.ZodString>;
        strokeWidth: z.ZodOptional<z.ZodNumber>;
        roughness: z.ZodOptional<z.ZodNumber>;
        opacity: z.ZodOptional<z.ZodNumber>;
        text: z.ZodOptional<z.ZodString>;
        fontSize: z.ZodOptional<z.ZodNumber>;
        fontFamily: z.ZodOptional<z.ZodNumber>;
        groupIds: z.ZodOptional<z.ZodArray<z.ZodString, "many">>;
        locked: z.ZodOptional<z.ZodBoolean>;
        angle: z.ZodOptional<z.ZodNumber>;
    }, "strip", z.ZodTypeAny, {
        type: "rectangle" | "ellipse" | "diamond" | "arrow" | "text" | "line" | "freedraw";
        x: number;
        y: number;
        text?: string | undefined;
        width?: number | undefined;
        height?: number | undefined;
        backgroundColor?: string | undefined;
        strokeColor?: string | undefined;
        strokeWidth?: number | undefined;
        roughness?: number | undefined;
        opacity?: number | undefined;
        fontSize?: number | undefined;
        fontFamily?: number | undefined;
        groupIds?: string[] | undefined;
        locked?: boolean | undefined;
        angle?: number | undefined;
        points?: {
            x: number;
            y: number;
        }[] | undefined;
    }, {
        type: "rectangle" | "ellipse" | "diamond" | "arrow" | "text" | "line" | "freedraw";
        x: number;
        y: number;
        text?: string | undefined;
        width?: number | undefined;
        height?: number | undefined;
        backgroundColor?: string | undefined;
        strokeColor?: string | undefined;
        strokeWidth?: number | undefined;
        roughness?: number | undefined;
        opacity?: number | undefined;
        fontSize?: number | undefined;
        fontFamily?: number | undefined;
        groupIds?: string[] | undefined;
        locked?: boolean | undefined;
        angle?: number | undefined;
        points?: {
            x: number;
            y: number;
        }[] | undefined;
    }>, "many">;
    title: z.ZodOptional<z.ZodString>;
    background: z.ZodOptional<z.ZodString>;
};
export type CreateViewArgs = z.infer<z.ZodObject<typeof CREATE_VIEW_SCHEMA>>;
/**
 * Handle the create_view tool call.
 * In MCP Apps mode, the elements are passed through to the widget via
 * the tool result. The widget receives them through ontoolinput/ontoolinputpartial
 * and renders them with streaming animations.
 *
 * In non-Apps mode (or as fallback), we also persist elements to the store
 * so they're accessible via the other 14 tools.
 */
export declare function handleCreateView(args: CreateViewArgs, persistToStore?: (elements: Record<string, unknown>[]) => Promise<void>): Promise<{
    content: Array<{
        type: 'text';
        text: string;
    }>;
}>;
//# sourceMappingURL=create-view.d.ts.map