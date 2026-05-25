import { GroupElementsSchema } from '../schemas/element.js';
import { generateId } from '../../shared/id.js';
export async function groupElementsTool(args, client) {
    const { elementIds } = GroupElementsSchema.parse(args);
    const groupId = generateId();
    let successCount = 0;
    for (const id of elementIds) {
        const element = await client.getElement(id);
        if (!element)
            continue;
        const existingGroupIds = element.groupIds ?? [];
        const updatedGroupIds = [...existingGroupIds, groupId];
        await client.updateElement(id, { groupIds: updatedGroupIds });
        successCount++;
    }
    return { success: true, groupId, elementIds, successCount };
}
