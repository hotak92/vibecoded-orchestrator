import { ElementIdSchema } from '../schemas/element.js';
export async function deleteElementTool(args, client) {
    const { id } = ElementIdSchema.parse(args);
    const deleted = await client.deleteElement(id);
    if (!deleted)
        throw new Error(`Element ${id} not found`);
    return { success: true, message: `Element ${id} deleted` };
}
