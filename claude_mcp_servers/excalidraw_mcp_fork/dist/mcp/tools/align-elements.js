import { AlignElementsSchema } from '../schemas/element.js';
export async function alignElementsTool(args, client) {
    const { elementIds, alignment } = AlignElementsSchema.parse(args);
    const elements = await Promise.all(elementIds.map(async (id) => {
        const el = await client.getElement(id);
        if (!el)
            throw new Error(`Element ${id} not found`);
        return el;
    }));
    switch (alignment) {
        case 'left': {
            const minX = Math.min(...elements.map((el) => el.x));
            for (const el of elements) {
                if (el.x !== minX) {
                    await client.updateElement(el.id, { x: minX });
                }
            }
            break;
        }
        case 'right': {
            const maxRight = Math.max(...elements.map((el) => el.x + (el.width ?? 0)));
            for (const el of elements) {
                const newX = maxRight - (el.width ?? 0);
                if (el.x !== newX) {
                    await client.updateElement(el.id, { x: newX });
                }
            }
            break;
        }
        case 'center': {
            const avgCenterX = elements.reduce((sum, el) => sum + el.x + (el.width ?? 0) / 2, 0) /
                elements.length;
            for (const el of elements) {
                const newX = avgCenterX - (el.width ?? 0) / 2;
                if (el.x !== newX) {
                    await client.updateElement(el.id, { x: newX });
                }
            }
            break;
        }
        case 'top': {
            const minY = Math.min(...elements.map((el) => el.y));
            for (const el of elements) {
                if (el.y !== minY) {
                    await client.updateElement(el.id, { y: minY });
                }
            }
            break;
        }
        case 'bottom': {
            const maxBottom = Math.max(...elements.map((el) => el.y + (el.height ?? 0)));
            for (const el of elements) {
                const newY = maxBottom - (el.height ?? 0);
                if (el.y !== newY) {
                    await client.updateElement(el.id, { y: newY });
                }
            }
            break;
        }
        case 'middle': {
            const avgCenterY = elements.reduce((sum, el) => sum + el.y + (el.height ?? 0) / 2, 0) /
                elements.length;
            for (const el of elements) {
                const newY = avgCenterY - (el.height ?? 0) / 2;
                if (el.y !== newY) {
                    await client.updateElement(el.id, { y: newY });
                }
            }
            break;
        }
    }
    return { success: true, aligned: true, alignment, elementIds };
}
