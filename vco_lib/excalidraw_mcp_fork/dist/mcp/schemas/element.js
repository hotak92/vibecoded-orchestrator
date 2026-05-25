import { z } from 'zod';
import { LIMITS } from './limits.js';
// ---------------------------------------------------------------------------
// Primitive schemas
// ---------------------------------------------------------------------------
/**
 * Validates CSS color values: hex (#rgb, #rrggbb, #rrggbbaa),
 * rgb()/rgba() functional notation, 'transparent', or CSS named colors.
 */
export const ColorSchema = z
    .string()
    .max(LIMITS.MAX_COLOR_LENGTH)
    .regex(/^(?:#(?:[0-9a-fA-F]{3}){1,2}(?:[0-9a-fA-F]{2})?|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*(?:0|1|0?\.\d+))?\s*\)|transparent|[a-zA-Z]{1,30})$/, 'Invalid color format')
    .optional();
export const CoordinateSchema = z
    .number()
    .min(LIMITS.MIN_COORDINATE)
    .max(LIMITS.MAX_COORDINATE)
    .finite();
export const DimensionSchema = z
    .number()
    .min(0)
    .max(LIMITS.MAX_DIMENSION)
    .finite()
    .optional();
export const PointSchema = z
    .object({
    x: CoordinateSchema,
    y: CoordinateSchema,
})
    .strict();
export const ElementTypeSchema = z.enum([
    'rectangle',
    'ellipse',
    'diamond',
    'arrow',
    'text',
    'line',
    'freedraw',
]);
// ---------------------------------------------------------------------------
// Element CRUD schemas
// ---------------------------------------------------------------------------
export const CreateElementSchema = z
    .object({
    type: ElementTypeSchema,
    x: CoordinateSchema,
    y: CoordinateSchema,
    width: DimensionSchema,
    height: DimensionSchema,
    points: z.array(PointSchema).max(LIMITS.MAX_POINTS).optional(),
    backgroundColor: ColorSchema,
    strokeColor: ColorSchema,
    strokeWidth: z
        .number()
        .min(0)
        .max(LIMITS.MAX_STROKE_WIDTH)
        .finite()
        .optional(),
    roughness: z
        .number()
        .min(0)
        .max(LIMITS.MAX_ROUGHNESS)
        .finite()
        .optional(),
    opacity: z
        .number()
        .min(LIMITS.MIN_OPACITY)
        .max(LIMITS.MAX_OPACITY)
        .finite()
        .optional(),
    text: z.string().max(LIMITS.MAX_TEXT_LENGTH).optional(),
    fontSize: z.number().min(1).max(LIMITS.MAX_FONT_SIZE).finite().optional(),
    fontFamily: z.number().int().min(1).max(4).optional(),
    groupIds: z
        .array(z.string().max(LIMITS.MAX_GROUP_ID_LENGTH))
        .max(LIMITS.MAX_GROUP_IDS)
        .optional(),
    locked: z.boolean().optional(),
    angle: z.number().min(-360).max(360).finite().optional(),
})
    .strict();
export const UpdateElementSchema = z
    .object({
    id: z.string().max(LIMITS.MAX_ID_LENGTH),
})
    .merge(CreateElementSchema.partial())
    .strict();
export const ElementIdSchema = z
    .object({
    id: z.string().max(LIMITS.MAX_ID_LENGTH),
})
    .strict();
export const ElementIdsSchema = z
    .object({
    elementIds: z
        .array(z.string().max(LIMITS.MAX_ID_LENGTH))
        .min(1)
        .max(LIMITS.MAX_ELEMENT_IDS),
})
    .strict();
export const BatchCreateSchema = z
    .object({
    elements: z
        .array(CreateElementSchema)
        .min(1)
        .max(LIMITS.MAX_BATCH_SIZE),
})
    .strict();
// ---------------------------------------------------------------------------
// Layout / grouping schemas
// ---------------------------------------------------------------------------
export const AlignElementsSchema = z
    .object({
    elementIds: z
        .array(z.string().max(LIMITS.MAX_ID_LENGTH))
        .min(2)
        .max(LIMITS.MAX_ELEMENT_IDS),
    alignment: z.enum([
        'left',
        'center',
        'right',
        'top',
        'middle',
        'bottom',
    ]),
})
    .strict();
export const DistributeElementsSchema = z
    .object({
    elementIds: z
        .array(z.string().max(LIMITS.MAX_ID_LENGTH))
        .min(3)
        .max(LIMITS.MAX_ELEMENT_IDS),
    direction: z.enum(['horizontal', 'vertical']),
})
    .strict();
export const GroupElementsSchema = z
    .object({
    elementIds: z
        .array(z.string().max(LIMITS.MAX_ID_LENGTH))
        .min(2)
        .max(LIMITS.MAX_ELEMENT_IDS),
})
    .strict();
export const GroupIdSchema = z
    .object({
    groupId: z.string().max(LIMITS.MAX_GROUP_ID_LENGTH),
})
    .strict();
// ---------------------------------------------------------------------------
// Query / resource schemas
// ---------------------------------------------------------------------------
export const QuerySchema = z
    .object({
    type: ElementTypeSchema.optional(),
    locked: z.boolean().optional(),
    groupId: z.string().max(LIMITS.MAX_GROUP_ID_LENGTH).optional(),
})
    .strict();
export const ResourceSchema = z
    .object({
    resource: z.enum(['scene', 'library', 'theme', 'elements']),
})
    .strict();
// ---------------------------------------------------------------------------
// Mermaid conversion schema
// ---------------------------------------------------------------------------
export const MermaidSchema = z
    .object({
    mermaidDiagram: z.string().min(1).max(LIMITS.MAX_MERMAID_LENGTH),
    config: z
        .object({
        startOnLoad: z.boolean().optional(),
        flowchart: z
            .object({})
            .strict()
            .optional(),
        themeVariables: z
            .object({})
            .strict()
            .optional(),
        maxEdges: z.number().int().min(1).max(1000).optional(),
        maxTextSize: z.number().int().min(1).max(100_000).optional(),
    })
        .strict()
        .optional(),
})
    .strict();
// ---------------------------------------------------------------------------
// Export schema
// ---------------------------------------------------------------------------
export const ExportSchema = z
    .object({
    format: z.enum(['png', 'svg']),
    elementIds: z
        .array(z.string().max(LIMITS.MAX_ID_LENGTH))
        .max(LIMITS.MAX_ELEMENT_IDS)
        .optional(),
    background: ColorSchema,
    padding: z.number().min(0).max(500).finite().optional(),
})
    .strict();
