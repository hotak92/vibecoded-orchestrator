import { ElementIdsSchema } from '../schemas/element.js';
export async function unlockElementsTool(args, client) {
    const { elementIds } = ElementIdsSchema.parse(args);
    let unlockedCount = 0;
    for (const id of elementIds) {
        await client.updateElement(id, { locked: false });
        unlockedCount++;
    }
    return { success: true, unlockedCount };
}
