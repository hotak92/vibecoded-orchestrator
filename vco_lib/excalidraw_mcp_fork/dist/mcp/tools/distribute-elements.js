import { DistributeElementsSchema } from '../schemas/element.js';
export async function distributeElementsTool(args, client) {
    const { elementIds, direction } = DistributeElementsSchema.parse(args);
    const elements = await Promise.all(elementIds.map(async (id) => {
        const el = await client.getElement(id);
        if (!el)
            throw new Error(`Element ${id} not found`);
        return el;
    }));
    if (direction === 'horizontal') {
        const sorted = [...elements].sort((a, b) => a.x - b.x);
        const first = sorted[0];
        const last = sorted[sorted.length - 1];
        const totalSpan = last.x + (last.width ?? 0) - first.x;
        const totalElementWidth = sorted.reduce((sum, el) => sum + (el.width ?? 0), 0);
        const gap = (totalSpan - totalElementWidth) / (sorted.length - 1);
        let currentX = first.x;
        for (const el of sorted) {
            if (el.x !== currentX) {
                await client.updateElement(el.id, { x: currentX });
            }
            currentX += (el.width ?? 0) + gap;
        }
    }
    else {
        const sorted = [...elements].sort((a, b) => a.y - b.y);
        const first = sorted[0];
        const last = sorted[sorted.length - 1];
        const totalSpan = last.y + (last.height ?? 0) - first.y;
        const totalElementHeight = sorted.reduce((sum, el) => sum + (el.height ?? 0), 0);
        const gap = (totalSpan - totalElementHeight) / (sorted.length - 1);
        let currentY = first.y;
        for (const el of sorted) {
            if (el.y !== currentY) {
                await client.updateElement(el.id, { y: currentY });
            }
            currentY += (el.height ?? 0) + gap;
        }
    }
    return { success: true, distributed: true, direction, elementIds };
}
