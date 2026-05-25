import { GroupIdSchema } from '../schemas/element.js';
export async function ungroupElementsTool(args, client) {
    const { groupId } = GroupIdSchema.parse(args);
    const allElements = await client.getAllElements();
    const grouped = allElements.filter((el) => el.groupIds && el.groupIds.includes(groupId));
    if (grouped.length === 0) {
        throw new Error(`No elements found with groupId ${groupId}`);
    }
    let ungroupedCount = 0;
    for (const element of grouped) {
        const updatedGroupIds = (element.groupIds ?? []).filter((gid) => gid !== groupId);
        await client.updateElement(element.id, { groupIds: updatedGroupIds });
        ungroupedCount++;
    }
    return { success: true, groupId, ungroupedCount };
}
