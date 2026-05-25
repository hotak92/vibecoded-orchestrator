import { z } from 'zod';
import { LIMITS } from '../schemas/limits.js';
const CoordZ = z.number().min(LIMITS.MIN_COORDINATE).max(LIMITS.MAX_COORDINATE).finite();
const DimZ = z.number().min(0).max(LIMITS.MAX_DIMENSION).finite().optional();
const ColorZ = z.string().max(LIMITS.MAX_COLOR_LENGTH).optional();
const PointZ = z.object({ x: CoordZ, y: CoordZ });
const ELEMENT_TYPES = [
    'rectangle', 'ellipse', 'diamond', 'arrow',
    'text', 'line', 'freedraw',
];
const viewElementSchema = z.object({
    type: z.enum(ELEMENT_TYPES),
    x: CoordZ,
    y: CoordZ,
    width: DimZ,
    height: DimZ,
    points: z.array(PointZ).max(LIMITS.MAX_POINTS).optional(),
    backgroundColor: ColorZ,
    strokeColor: ColorZ,
    strokeWidth: z.number().min(0).max(LIMITS.MAX_STROKE_WIDTH).finite().optional(),
    roughness: z.number().min(0).max(LIMITS.MAX_ROUGHNESS).finite().optional(),
    opacity: z.number().min(LIMITS.MIN_OPACITY).max(LIMITS.MAX_OPACITY).finite().optional(),
    text: z.string().max(LIMITS.MAX_TEXT_LENGTH).optional(),
    fontSize: z.number().min(1).max(LIMITS.MAX_FONT_SIZE).finite().optional(),
    fontFamily: z.number().int().min(1).max(4).optional(),
    groupIds: z.array(z.string().max(LIMITS.MAX_GROUP_ID_LENGTH)).max(LIMITS.MAX_GROUP_IDS).optional(),
    locked: z.boolean().optional(),
    angle: z.number().min(-360).max(360).finite().optional(),
});
export const CREATE_VIEW_SCHEMA = {
    elements: z.array(viewElementSchema).min(1).max(LIMITS.MAX_BATCH_SIZE),
    title: z.string().max(200).optional(),
    background: ColorZ,
};
/**
 * Handle the create_view tool call.
 * In MCP Apps mode, the elements are passed through to the widget via
 * the tool result. The widget receives them through ontoolinput/ontoolinputpartial
 * and renders them with streaming animations.
 *
 * In non-Apps mode (or as fallback), we also persist elements to the store
 * so they're accessible via the other 14 tools.
 */
export async function handleCreateView(args, persistToStore) {
    const { elements, title } = args;
    // Persist elements to the store so other tools can reference them
    if (persistToStore) {
        await persistToStore(elements);
    }
    const result = {
        title: title ?? 'Excalidraw Diagram',
        elementCount: elements.length,
        elements,
    };
    return {
        content: [{
                type: 'text',
                text: JSON.stringify(result, null, 2),
            }],
    };
}
