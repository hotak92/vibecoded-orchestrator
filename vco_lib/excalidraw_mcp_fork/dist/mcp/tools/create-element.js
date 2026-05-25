import { CreateElementSchema } from '../schemas/element.js';
export async function createElementTool(args, client) {
    const data = CreateElementSchema.parse(args);
    const element = await client.createElement(data);
    return { success: true, element };
}
