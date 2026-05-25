import { ElementIdsSchema } from '../schemas/element.js';
export async function lockElementsTool(args, client) {
    const { elementIds } = ElementIdsSchema.parse(args);
    let lockedCount = 0;
    for (const id of elementIds) {
        await client.updateElement(id, { locked: true });
        lockedCount++;
    }
    return { success: true, lockedCount };
}
