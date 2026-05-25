import { ExportSchema } from '../schemas/element.js';
export async function exportSceneTool(args, client) {
    const { format, elementIds, background, padding } = ExportSchema.parse(args);
    const result = await client.exportScene(format, {
        elementIds,
        background: background ?? undefined,
        padding,
    });
    return result;
}
