import { ResourceSchema } from '../schemas/element.js';
export async function getResourceTool(args, client) {
    const { resource } = ResourceSchema.parse(args);
    switch (resource) {
        case 'elements': {
            const elements = await client.getAllElements();
            return { success: true, resource: 'elements', elements, count: elements.length };
        }
        case 'scene':
            return {
                success: true,
                resource: 'scene',
                scene: { theme: 'light', viewport: { x: 0, y: 0, zoom: 1 } },
            };
        case 'theme':
            return { success: true, resource: 'theme', theme: 'light' };
        case 'library':
            return { success: true, resource: 'library', items: [] };
    }
}
