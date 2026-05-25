import { z } from 'zod';
export declare const ServerMessageSchema: z.ZodDiscriminatedUnion<"type", [z.ZodObject<{
    type: z.ZodLiteral<"initial_elements">;
    elements: z.ZodArray<z.ZodRecord<z.ZodString, z.ZodUnknown>, "many">;
}, "strip", z.ZodTypeAny, {
    type: "initial_elements";
    elements: Record<string, unknown>[];
}, {
    type: "initial_elements";
    elements: Record<string, unknown>[];
}>, z.ZodObject<{
    type: z.ZodLiteral<"element_created">;
    element: z.ZodRecord<z.ZodString, z.ZodUnknown>;
}, "strip", z.ZodTypeAny, {
    type: "element_created";
    element: Record<string, unknown>;
}, {
    type: "element_created";
    element: Record<string, unknown>;
}>, z.ZodObject<{
    type: z.ZodLiteral<"element_updated">;
    element: z.ZodRecord<z.ZodString, z.ZodUnknown>;
}, "strip", z.ZodTypeAny, {
    type: "element_updated";
    element: Record<string, unknown>;
}, {
    type: "element_updated";
    element: Record<string, unknown>;
}>, z.ZodObject<{
    type: z.ZodLiteral<"element_deleted">;
    elementId: z.ZodString;
}, "strip", z.ZodTypeAny, {
    type: "element_deleted";
    elementId: string;
}, {
    type: "element_deleted";
    elementId: string;
}>, z.ZodObject<{
    type: z.ZodLiteral<"elements_batch_created">;
    elements: z.ZodArray<z.ZodRecord<z.ZodString, z.ZodUnknown>, "many">;
}, "strip", z.ZodTypeAny, {
    type: "elements_batch_created";
    elements: Record<string, unknown>[];
}, {
    type: "elements_batch_created";
    elements: Record<string, unknown>[];
}>, z.ZodObject<{
    type: z.ZodLiteral<"sync_status">;
    elementCount: z.ZodNumber;
    timestamp: z.ZodString;
}, "strip", z.ZodTypeAny, {
    type: "sync_status";
    elementCount: number;
    timestamp: string;
}, {
    type: "sync_status";
    elementCount: number;
    timestamp: string;
}>, z.ZodObject<{
    type: z.ZodLiteral<"mermaid_convert">;
    mermaidDiagram: z.ZodString;
    config: z.ZodOptional<z.ZodRecord<z.ZodString, z.ZodUnknown>>;
    timestamp: z.ZodString;
}, "strip", z.ZodTypeAny, {
    type: "mermaid_convert";
    timestamp: string;
    mermaidDiagram: string;
    config?: Record<string, unknown> | undefined;
}, {
    type: "mermaid_convert";
    timestamp: string;
    mermaidDiagram: string;
    config?: Record<string, unknown> | undefined;
}>, z.ZodObject<{
    type: z.ZodLiteral<"error">;
    error: z.ZodString;
}, "strip", z.ZodTypeAny, {
    error: string;
    type: "error";
}, {
    error: string;
    type: "error";
}>]>;
export declare const ClientMessageSchema: z.ZodDiscriminatedUnion<"type", [z.ZodObject<{
    type: z.ZodLiteral<"sync_request">;
    elements: z.ZodArray<z.ZodRecord<z.ZodString, z.ZodUnknown>, "many">;
    timestamp: z.ZodString;
}, "strip", z.ZodTypeAny, {
    type: "sync_request";
    elements: Record<string, unknown>[];
    timestamp: string;
}, {
    type: "sync_request";
    elements: Record<string, unknown>[];
    timestamp: string;
}>, z.ZodObject<{
    type: z.ZodLiteral<"ping">;
}, "strip", z.ZodTypeAny, {
    type: "ping";
}, {
    type: "ping";
}>]>;
export type ServerMessage = z.infer<typeof ServerMessageSchema>;
export type ClientMessage = z.infer<typeof ClientMessageSchema>;
//# sourceMappingURL=protocol.d.ts.map