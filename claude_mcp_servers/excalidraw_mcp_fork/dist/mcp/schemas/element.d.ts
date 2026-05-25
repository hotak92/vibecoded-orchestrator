import { z } from 'zod';
/**
 * Validates CSS color values: hex (#rgb, #rrggbb, #rrggbbaa),
 * rgb()/rgba() functional notation, 'transparent', or CSS named colors.
 */
export declare const ColorSchema: z.ZodOptional<z.ZodString>;
export declare const CoordinateSchema: z.ZodNumber;
export declare const DimensionSchema: z.ZodOptional<z.ZodNumber>;
export declare const PointSchema: z.ZodObject<{
    x: z.ZodNumber;
    y: z.ZodNumber;
}, "strict", z.ZodTypeAny, {
    x: number;
    y: number;
}, {
    x: number;
    y: number;
}>;
export declare const ElementTypeSchema: z.ZodEnum<["rectangle", "ellipse", "diamond", "arrow", "text", "line", "freedraw"]>;
export declare const CreateElementSchema: z.ZodObject<{
    type: z.ZodEnum<["rectangle", "ellipse", "diamond", "arrow", "text", "line", "freedraw"]>;
    x: z.ZodNumber;
    y: z.ZodNumber;
    width: z.ZodOptional<z.ZodNumber>;
    height: z.ZodOptional<z.ZodNumber>;
    points: z.ZodOptional<z.ZodArray<z.ZodObject<{
        x: z.ZodNumber;
        y: z.ZodNumber;
    }, "strict", z.ZodTypeAny, {
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
}, "strict", z.ZodTypeAny, {
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
}>;
export declare const UpdateElementSchema: z.ZodObject<{
    id: z.ZodString;
} & {
    type: z.ZodOptional<z.ZodEnum<["rectangle", "ellipse", "diamond", "arrow", "text", "line", "freedraw"]>>;
    x: z.ZodOptional<z.ZodNumber>;
    y: z.ZodOptional<z.ZodNumber>;
    width: z.ZodOptional<z.ZodOptional<z.ZodNumber>>;
    height: z.ZodOptional<z.ZodOptional<z.ZodNumber>>;
    points: z.ZodOptional<z.ZodOptional<z.ZodArray<z.ZodObject<{
        x: z.ZodNumber;
        y: z.ZodNumber;
    }, "strict", z.ZodTypeAny, {
        x: number;
        y: number;
    }, {
        x: number;
        y: number;
    }>, "many">>>;
    backgroundColor: z.ZodOptional<z.ZodOptional<z.ZodString>>;
    strokeColor: z.ZodOptional<z.ZodOptional<z.ZodString>>;
    strokeWidth: z.ZodOptional<z.ZodOptional<z.ZodNumber>>;
    roughness: z.ZodOptional<z.ZodOptional<z.ZodNumber>>;
    opacity: z.ZodOptional<z.ZodOptional<z.ZodNumber>>;
    text: z.ZodOptional<z.ZodOptional<z.ZodString>>;
    fontSize: z.ZodOptional<z.ZodOptional<z.ZodNumber>>;
    fontFamily: z.ZodOptional<z.ZodOptional<z.ZodNumber>>;
    groupIds: z.ZodOptional<z.ZodOptional<z.ZodArray<z.ZodString, "many">>>;
    locked: z.ZodOptional<z.ZodOptional<z.ZodBoolean>>;
    angle: z.ZodOptional<z.ZodOptional<z.ZodNumber>>;
}, "strict", z.ZodTypeAny, {
    id: string;
    type?: "rectangle" | "ellipse" | "diamond" | "arrow" | "text" | "line" | "freedraw" | undefined;
    text?: string | undefined;
    x?: number | undefined;
    y?: number | undefined;
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
    id: string;
    type?: "rectangle" | "ellipse" | "diamond" | "arrow" | "text" | "line" | "freedraw" | undefined;
    text?: string | undefined;
    x?: number | undefined;
    y?: number | undefined;
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
}>;
export declare const ElementIdSchema: z.ZodObject<{
    id: z.ZodString;
}, "strict", z.ZodTypeAny, {
    id: string;
}, {
    id: string;
}>;
export declare const ElementIdsSchema: z.ZodObject<{
    elementIds: z.ZodArray<z.ZodString, "many">;
}, "strict", z.ZodTypeAny, {
    elementIds: string[];
}, {
    elementIds: string[];
}>;
export declare const BatchCreateSchema: z.ZodObject<{
    elements: z.ZodArray<z.ZodObject<{
        type: z.ZodEnum<["rectangle", "ellipse", "diamond", "arrow", "text", "line", "freedraw"]>;
        x: z.ZodNumber;
        y: z.ZodNumber;
        width: z.ZodOptional<z.ZodNumber>;
        height: z.ZodOptional<z.ZodNumber>;
        points: z.ZodOptional<z.ZodArray<z.ZodObject<{
            x: z.ZodNumber;
            y: z.ZodNumber;
        }, "strict", z.ZodTypeAny, {
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
    }, "strict", z.ZodTypeAny, {
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
}, "strict", z.ZodTypeAny, {
    elements: {
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
    }[];
}, {
    elements: {
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
    }[];
}>;
export declare const AlignElementsSchema: z.ZodObject<{
    elementIds: z.ZodArray<z.ZodString, "many">;
    alignment: z.ZodEnum<["left", "center", "right", "top", "middle", "bottom"]>;
}, "strict", z.ZodTypeAny, {
    elementIds: string[];
    alignment: "left" | "center" | "right" | "top" | "middle" | "bottom";
}, {
    elementIds: string[];
    alignment: "left" | "center" | "right" | "top" | "middle" | "bottom";
}>;
export declare const DistributeElementsSchema: z.ZodObject<{
    elementIds: z.ZodArray<z.ZodString, "many">;
    direction: z.ZodEnum<["horizontal", "vertical"]>;
}, "strict", z.ZodTypeAny, {
    elementIds: string[];
    direction: "horizontal" | "vertical";
}, {
    elementIds: string[];
    direction: "horizontal" | "vertical";
}>;
export declare const GroupElementsSchema: z.ZodObject<{
    elementIds: z.ZodArray<z.ZodString, "many">;
}, "strict", z.ZodTypeAny, {
    elementIds: string[];
}, {
    elementIds: string[];
}>;
export declare const GroupIdSchema: z.ZodObject<{
    groupId: z.ZodString;
}, "strict", z.ZodTypeAny, {
    groupId: string;
}, {
    groupId: string;
}>;
export declare const QuerySchema: z.ZodObject<{
    type: z.ZodOptional<z.ZodEnum<["rectangle", "ellipse", "diamond", "arrow", "text", "line", "freedraw"]>>;
    locked: z.ZodOptional<z.ZodBoolean>;
    groupId: z.ZodOptional<z.ZodString>;
}, "strict", z.ZodTypeAny, {
    type?: "rectangle" | "ellipse" | "diamond" | "arrow" | "text" | "line" | "freedraw" | undefined;
    locked?: boolean | undefined;
    groupId?: string | undefined;
}, {
    type?: "rectangle" | "ellipse" | "diamond" | "arrow" | "text" | "line" | "freedraw" | undefined;
    locked?: boolean | undefined;
    groupId?: string | undefined;
}>;
export declare const ResourceSchema: z.ZodObject<{
    resource: z.ZodEnum<["scene", "library", "theme", "elements"]>;
}, "strict", z.ZodTypeAny, {
    resource: "elements" | "theme" | "scene" | "library";
}, {
    resource: "elements" | "theme" | "scene" | "library";
}>;
export declare const MermaidSchema: z.ZodObject<{
    mermaidDiagram: z.ZodString;
    config: z.ZodOptional<z.ZodObject<{
        startOnLoad: z.ZodOptional<z.ZodBoolean>;
        flowchart: z.ZodOptional<z.ZodObject<{}, "strict", z.ZodTypeAny, {}, {}>>;
        themeVariables: z.ZodOptional<z.ZodObject<{}, "strict", z.ZodTypeAny, {}, {}>>;
        maxEdges: z.ZodOptional<z.ZodNumber>;
        maxTextSize: z.ZodOptional<z.ZodNumber>;
    }, "strict", z.ZodTypeAny, {
        startOnLoad?: boolean | undefined;
        flowchart?: {} | undefined;
        themeVariables?: {} | undefined;
        maxEdges?: number | undefined;
        maxTextSize?: number | undefined;
    }, {
        startOnLoad?: boolean | undefined;
        flowchart?: {} | undefined;
        themeVariables?: {} | undefined;
        maxEdges?: number | undefined;
        maxTextSize?: number | undefined;
    }>>;
}, "strict", z.ZodTypeAny, {
    mermaidDiagram: string;
    config?: {
        startOnLoad?: boolean | undefined;
        flowchart?: {} | undefined;
        themeVariables?: {} | undefined;
        maxEdges?: number | undefined;
        maxTextSize?: number | undefined;
    } | undefined;
}, {
    mermaidDiagram: string;
    config?: {
        startOnLoad?: boolean | undefined;
        flowchart?: {} | undefined;
        themeVariables?: {} | undefined;
        maxEdges?: number | undefined;
        maxTextSize?: number | undefined;
    } | undefined;
}>;
export declare const ExportSchema: z.ZodObject<{
    format: z.ZodEnum<["png", "svg"]>;
    elementIds: z.ZodOptional<z.ZodArray<z.ZodString, "many">>;
    background: z.ZodOptional<z.ZodString>;
    padding: z.ZodOptional<z.ZodNumber>;
}, "strict", z.ZodTypeAny, {
    format: "png" | "svg";
    elementIds?: string[] | undefined;
    background?: string | undefined;
    padding?: number | undefined;
}, {
    format: "png" | "svg";
    elementIds?: string[] | undefined;
    background?: string | undefined;
    padding?: number | undefined;
}>;
export type Color = z.infer<typeof ColorSchema>;
export type Coordinate = z.infer<typeof CoordinateSchema>;
export type Dimension = z.infer<typeof DimensionSchema>;
export type Point = z.infer<typeof PointSchema>;
export type ElementType = z.infer<typeof ElementTypeSchema>;
export type CreateElement = z.infer<typeof CreateElementSchema>;
export type UpdateElement = z.infer<typeof UpdateElementSchema>;
export type ElementId = z.infer<typeof ElementIdSchema>;
export type ElementIds = z.infer<typeof ElementIdsSchema>;
export type BatchCreate = z.infer<typeof BatchCreateSchema>;
export type AlignElements = z.infer<typeof AlignElementsSchema>;
export type DistributeElements = z.infer<typeof DistributeElementsSchema>;
export type GroupElements = z.infer<typeof GroupElementsSchema>;
export type GroupId = z.infer<typeof GroupIdSchema>;
export type Query = z.infer<typeof QuerySchema>;
export type Resource = z.infer<typeof ResourceSchema>;
export type Mermaid = z.infer<typeof MermaidSchema>;
export type Export = z.infer<typeof ExportSchema>;
//# sourceMappingURL=element.d.ts.map