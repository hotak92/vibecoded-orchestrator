import { BatchCreateSchema } from '../schemas/element.js';
export async function batchCreateTool(args, client) {
    const { elements } = BatchCreateSchema.parse(args);
    const created = await client.batchCreate(elements);
    return { success: true, elements: created, count: created.length };
}
