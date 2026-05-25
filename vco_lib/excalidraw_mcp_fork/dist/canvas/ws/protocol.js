import { z } from 'zod';
const MAX_ELEMENTS = 10_000;
const MAX_BATCH = 100;
const MAX_ID = 64;
const MAX_MERMAID = 50_000;
export const ServerMessageSchema = z.discriminatedUnion('type', [
    z.object({
        type: z.literal('initial_elements'),
        elements: z.array(z.record(z.string(), z.unknown())).max(MAX_ELEMENTS),
    }),
    z.object({
        type: z.literal('element_created'),
        element: z.record(z.string(), z.unknown()),
    }),
    z.object({
        type: z.literal('element_updated'),
        element: z.record(z.string(), z.unknown()),
    }),
    z.object({
        type: z.literal('element_deleted'),
        elementId: z.string().max(MAX_ID),
    }),
    z.object({
        type: z.literal('elements_batch_created'),
        elements: z.array(z.record(z.string(), z.unknown())).max(MAX_BATCH),
    }),
    z.object({
        type: z.literal('sync_status'),
        elementCount: z.number().int().min(0),
        timestamp: z.string(),
    }),
    z.object({
        type: z.literal('mermaid_convert'),
        mermaidDiagram: z.string().max(MAX_MERMAID),
        config: z.record(z.string(), z.unknown()).optional(),
        timestamp: z.string(),
    }),
    z.object({
        type: z.literal('error'),
        error: z.string().max(500),
    }),
]);
export const ClientMessageSchema = z.discriminatedUnion('type', [
    z.object({
        type: z.literal('sync_request'),
        elements: z.array(z.record(z.string(), z.unknown())).max(MAX_ELEMENTS),
        timestamp: z.string(),
    }),
    z.object({
        type: z.literal('ping'),
    }),
]);
