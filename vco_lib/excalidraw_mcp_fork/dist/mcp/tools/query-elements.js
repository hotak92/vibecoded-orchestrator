import { QuerySchema } from '../schemas/element.js';
export async function queryElementsTool(args, client) {
    const filter = QuerySchema.parse(args);
    const searchFilter = {};
    if (filter.type)
        searchFilter.type = filter.type;
    if (filter.locked !== undefined)
        searchFilter.locked = String(filter.locked);
    if (filter.groupId)
        searchFilter.groupId = filter.groupId;
    const elements = await client.searchElements(searchFilter);
    return { success: true, elements, count: elements.length };
}
