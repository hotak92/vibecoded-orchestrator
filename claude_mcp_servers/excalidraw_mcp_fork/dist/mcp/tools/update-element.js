import { UpdateElementSchema } from '../schemas/element.js';
export async function updateElementTool(args, client) {
    const { id, ...data } = UpdateElementSchema.parse(args);
    const element = await client.updateElement(id, data);
    return { success: true, element };
}
