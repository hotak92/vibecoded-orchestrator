#!/usr/bin/env node
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadConfig } from '../shared/config.js';
import { createLogger } from '../shared/logger.js';
import { CanvasClient } from './canvas-client.js';
import { StandaloneStore } from './apps/standalone-store.js';
import { CanvasClientAdapter } from './apps/canvas-client-adapter.js';
import { registerMcpApps } from './apps/register.js';
import { LIMITS } from './schemas/limits.js';
const logger = createLogger('mcp');
const ELEMENT_TYPES = [
    'rectangle', 'ellipse', 'diamond', 'arrow',
    'text', 'line', 'freedraw',
];
async function detectMode(config) {
    if (!config.STANDALONE_MODE)
        return 'connected';
    // Try to reach the canvas server
    const canvasClient = new CanvasClient(config);
    const reachable = await canvasClient.healthCheck();
    if (reachable) {
        logger.info('Canvas server reachable, running in connected mode');
        return 'connected';
    }
    logger.info('Canvas server not reachable, running in standalone mode');
    return 'standalone';
}
async function main() {
    const config = loadConfig();
    const mode = await detectMode(config);
    // Create the right client based on mode
    let client;
    let standaloneStore = null;
    if (mode === 'connected') {
        client = new CanvasClient(config);
    }
    else {
        standaloneStore = new StandaloneStore(config.MAX_ELEMENTS);
        client = new CanvasClientAdapter(standaloneStore);
    }
    const server = new McpServer({
        name: 'excalidraw-mcp-server',
        version: '2.0.0',
    });
    // Register MCP Apps tools (create_view, read_me) and widget resource
    const __dirname = path.dirname(fileURLToPath(import.meta.url));
    registerMcpApps(server, {
        getWidgetHtml: async () => {
            const widgetPath = path.resolve(__dirname, '../widget/index.html');
            try {
                return await fs.promises.readFile(widgetPath, 'utf-8');
            }
            catch {
                return '<html><body><p>Widget not built. Run npm run build:widget</p></body></html>';
            }
        },
        persistToStore: async (elements) => {
            await client.batchCreate(elements);
        },
    });
    // Shared schema fragments
    const CoordZ = z.number().min(LIMITS.MIN_COORDINATE).max(LIMITS.MAX_COORDINATE).finite();
    const DimZ = z.number().min(0).max(LIMITS.MAX_DIMENSION).finite().optional();
    const ColorZ = z.string().max(LIMITS.MAX_COLOR_LENGTH).optional();
    const IdZ = z.string().max(LIMITS.MAX_ID_LENGTH);
    const IdsZ = z.array(IdZ).min(1).max(LIMITS.MAX_ELEMENT_IDS);
    const PointZ = z.object({ x: CoordZ, y: CoordZ });
    const elementFields = {
        type: z.enum(ELEMENT_TYPES),
        x: CoordZ,
        y: CoordZ,
        width: DimZ,
        height: DimZ,
        points: z.array(PointZ).max(LIMITS.MAX_POINTS).optional(),
        backgroundColor: ColorZ,
        strokeColor: ColorZ,
        strokeWidth: z.number().min(0).max(LIMITS.MAX_STROKE_WIDTH).finite().optional(),
        roughness: z.number().min(0).max(LIMITS.MAX_ROUGHNESS).finite().optional(),
        opacity: z.number().min(LIMITS.MIN_OPACITY).max(LIMITS.MAX_OPACITY).finite().optional(),
        text: z.string().max(LIMITS.MAX_TEXT_LENGTH).optional(),
        fontSize: z.number().min(1).max(LIMITS.MAX_FONT_SIZE).finite().optional(),
        fontFamily: z.number().int().min(1).max(4).optional(),
        groupIds: z.array(z.string().max(LIMITS.MAX_GROUP_ID_LENGTH)).max(LIMITS.MAX_GROUP_IDS).optional(),
        locked: z.boolean().optional(),
        angle: z.number().min(-360).max(360).finite().optional(),
    };
    const partialElementFields = Object.fromEntries(Object.entries(elementFields).map(([k, v]) => [k, v.optional()]));
    // --- Tool: create_element ---
    server.tool('create_element', 'Create a single Excalidraw element on the canvas', elementFields, async ({ type, x, y, ...rest }) => {
        try {
            const element = await client.createElement({ type, x, y, ...rest });
            return { content: [{ type: 'text', text: JSON.stringify(element, null, 2) }] };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: update_element ---
    server.tool('update_element', 'Update an existing Excalidraw element by ID', { id: IdZ, ...partialElementFields }, async ({ id, ...data }) => {
        try {
            const clean = Object.fromEntries(Object.entries(data).filter(([_, v]) => v !== undefined));
            const element = await client.updateElement(id, clean);
            return { content: [{ type: 'text', text: JSON.stringify(element, null, 2) }] };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: delete_element ---
    server.tool('delete_element', 'Delete an Excalidraw element by ID', { id: IdZ }, async ({ id }) => {
        try {
            const deleted = await client.deleteElement(id);
            if (!deleted)
                throw new Error(`Element ${id} not found`);
            return { content: [{ type: 'text', text: `Element ${id} deleted` }] };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: query_elements ---
    server.tool('query_elements', 'Search for elements by type, locked status, or group ID', {
        type: z.enum(ELEMENT_TYPES).optional(),
        locked: z.boolean().optional(),
        groupId: z.string().max(LIMITS.MAX_GROUP_ID_LENGTH).optional(),
    }, async (filter) => {
        try {
            const searchFilter = {};
            if (filter.type)
                searchFilter.type = filter.type;
            if (filter.locked !== undefined)
                searchFilter.locked = String(filter.locked);
            if (filter.groupId)
                searchFilter.groupId = filter.groupId;
            const elements = await client.searchElements(searchFilter);
            return {
                content: [{
                        type: 'text',
                        text: JSON.stringify({ elements, count: elements.length }, null, 2),
                    }],
            };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: get_resource ---
    server.tool('get_resource', 'Get scene state, elements, theme, or library', { resource: z.enum(['scene', 'library', 'theme', 'elements']) }, async ({ resource }) => {
        try {
            let result;
            switch (resource) {
                case 'elements': {
                    const elements = await client.getAllElements();
                    result = { elements, count: elements.length };
                    break;
                }
                case 'scene':
                    result = { theme: 'light', viewport: { x: 0, y: 0, zoom: 1 } };
                    break;
                case 'theme':
                    result = { theme: 'light' };
                    break;
                case 'library':
                    result = { items: [] };
                    break;
            }
            return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: batch_create_elements ---
    server.tool('batch_create_elements', `Create multiple elements at once (max ${LIMITS.MAX_BATCH_SIZE})`, {
        elements: z.array(z.object(elementFields)).min(1).max(LIMITS.MAX_BATCH_SIZE),
    }, async ({ elements }) => {
        try {
            const created = await client.batchCreate(elements);
            return {
                content: [{
                        type: 'text',
                        text: JSON.stringify({ elements: created, count: created.length }, null, 2),
                    }],
            };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: group_elements ---
    server.tool('group_elements', 'Group multiple elements together', { elementIds: z.array(IdZ).min(2).max(LIMITS.MAX_ELEMENT_IDS) }, async ({ elementIds }) => {
        try {
            const { generateId } = await import('../shared/id.js');
            const groupId = generateId();
            let successCount = 0;
            for (const eid of elementIds) {
                const el = await client.getElement(eid);
                if (!el)
                    continue;
                const existingGroups = el.groupIds ?? [];
                await client.updateElement(eid, {
                    groupIds: [...existingGroups, groupId],
                });
                successCount++;
            }
            return {
                content: [{
                        type: 'text',
                        text: JSON.stringify({ groupId, elementIds, successCount }, null, 2),
                    }],
            };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: ungroup_elements ---
    server.tool('ungroup_elements', 'Remove elements from a group by group ID', { groupId: z.string().max(LIMITS.MAX_GROUP_ID_LENGTH) }, async ({ groupId }) => {
        try {
            const all = await client.getAllElements();
            const inGroup = all.filter(e => e.groupIds?.includes(groupId));
            if (inGroup.length === 0) {
                throw new Error(`No elements found with groupId ${groupId}`);
            }
            for (const el of inGroup) {
                const newGroups = (el.groupIds ?? []).filter(g => g !== groupId);
                await client.updateElement(el.id, { groupIds: newGroups });
            }
            return {
                content: [{
                        type: 'text',
                        text: JSON.stringify({ groupId, ungroupedCount: inGroup.length }, null, 2),
                    }],
            };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: align_elements ---
    server.tool('align_elements', 'Align elements (left, center, right, top, middle, bottom)', {
        elementIds: z.array(IdZ).min(2).max(LIMITS.MAX_ELEMENT_IDS),
        alignment: z.enum(['left', 'center', 'right', 'top', 'middle', 'bottom']),
    }, async ({ elementIds, alignment }) => {
        try {
            const elements = [];
            for (const eid of elementIds) {
                const el = await client.getElement(eid);
                if (!el)
                    throw new Error(`Element ${eid} not found`);
                if (el.locked)
                    throw new Error(`Element ${eid} is locked`);
                elements.push(el);
            }
            switch (alignment) {
                case 'left': {
                    const minX = Math.min(...elements.map(e => e.x));
                    for (const el of elements)
                        await client.updateElement(el.id, { x: minX });
                    break;
                }
                case 'right': {
                    const maxRight = Math.max(...elements.map(e => e.x + (e.width ?? 0)));
                    for (const el of elements)
                        await client.updateElement(el.id, { x: maxRight - (el.width ?? 0) });
                    break;
                }
                case 'center': {
                    const centers = elements.map(e => e.x + (e.width ?? 0) / 2);
                    const avg = centers.reduce((a, b) => a + b, 0) / centers.length;
                    for (const el of elements)
                        await client.updateElement(el.id, { x: avg - (el.width ?? 0) / 2 });
                    break;
                }
                case 'top': {
                    const minY = Math.min(...elements.map(e => e.y));
                    for (const el of elements)
                        await client.updateElement(el.id, { y: minY });
                    break;
                }
                case 'bottom': {
                    const maxBottom = Math.max(...elements.map(e => e.y + (e.height ?? 0)));
                    for (const el of elements)
                        await client.updateElement(el.id, { y: maxBottom - (el.height ?? 0) });
                    break;
                }
                case 'middle': {
                    const middles = elements.map(e => e.y + (e.height ?? 0) / 2);
                    const avgY = middles.reduce((a, b) => a + b, 0) / middles.length;
                    for (const el of elements)
                        await client.updateElement(el.id, { y: avgY - (el.height ?? 0) / 2 });
                    break;
                }
            }
            return {
                content: [{
                        type: 'text',
                        text: JSON.stringify({ aligned: true, alignment, elementIds }, null, 2),
                    }],
            };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: distribute_elements ---
    server.tool('distribute_elements', 'Distribute elements evenly (horizontal or vertical)', {
        elementIds: z.array(IdZ).min(3).max(LIMITS.MAX_ELEMENT_IDS),
        direction: z.enum(['horizontal', 'vertical']),
    }, async ({ elementIds, direction }) => {
        try {
            const elements = [];
            for (const eid of elementIds) {
                const el = await client.getElement(eid);
                if (!el)
                    throw new Error(`Element ${eid} not found`);
                if (el.locked)
                    throw new Error(`Element ${eid} is locked`);
                elements.push(el);
            }
            if (direction === 'horizontal') {
                const sorted = [...elements].sort((a, b) => a.x - b.x);
                const first = sorted[0];
                const last = sorted[sorted.length - 1];
                const totalSpan = (last.x + (last.width ?? 0)) - first.x;
                const totalWidth = sorted.reduce((s, e) => s + (e.width ?? 0), 0);
                const gap = (totalSpan - totalWidth) / (sorted.length - 1);
                let currentX = first.x;
                for (const el of sorted) {
                    await client.updateElement(el.id, { x: currentX });
                    currentX += (el.width ?? 0) + gap;
                }
            }
            else {
                const sorted = [...elements].sort((a, b) => a.y - b.y);
                const first = sorted[0];
                const last = sorted[sorted.length - 1];
                const totalSpan = (last.y + (last.height ?? 0)) - first.y;
                const totalHeight = sorted.reduce((s, e) => s + (e.height ?? 0), 0);
                const gap = (totalSpan - totalHeight) / (sorted.length - 1);
                let currentY = first.y;
                for (const el of sorted) {
                    await client.updateElement(el.id, { y: currentY });
                    currentY += (el.height ?? 0) + gap;
                }
            }
            return {
                content: [{
                        type: 'text',
                        text: JSON.stringify({ distributed: true, direction, elementIds }, null, 2),
                    }],
            };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: lock_elements ---
    server.tool('lock_elements', 'Lock elements to prevent modification', { elementIds: IdsZ }, async ({ elementIds }) => {
        try {
            let count = 0;
            for (const eid of elementIds) {
                await client.updateElement(eid, { locked: true });
                count++;
            }
            return { content: [{ type: 'text', text: JSON.stringify({ lockedCount: count }, null, 2) }] };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: unlock_elements ---
    server.tool('unlock_elements', 'Unlock elements to allow modification', { elementIds: IdsZ }, async ({ elementIds }) => {
        try {
            let count = 0;
            for (const eid of elementIds) {
                await client.updateElement(eid, { locked: false });
                count++;
            }
            return { content: [{ type: 'text', text: JSON.stringify({ unlockedCount: count }, null, 2) }] };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: create_from_mermaid ---
    server.tool('create_from_mermaid', 'Convert a Mermaid diagram to Excalidraw elements', {
        mermaidDiagram: z.string().min(1).max(LIMITS.MAX_MERMAID_LENGTH),
        config: z.object({
            startOnLoad: z.boolean().optional(),
            flowchart: z.object({ curve: z.enum(['linear', 'basis']).optional() }).optional(),
            themeVariables: z.object({ fontSize: z.string().max(10).optional() }).optional(),
            maxEdges: z.number().int().min(1).max(1000).optional(),
            maxTextSize: z.number().int().min(1).max(100000).optional(),
        }).optional(),
    }, async ({ mermaidDiagram, config }) => {
        try {
            await client.convertMermaid(mermaidDiagram, config);
            return {
                content: [{
                        type: 'text',
                        text: 'Mermaid conversion broadcast to canvas. Elements will appear when a frontend client is connected.',
                    }],
            };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // --- Tool: export_scene ---
    server.tool('export_scene', 'Export the canvas as PNG or SVG', {
        format: z.enum(['png', 'svg']),
        elementIds: z.array(IdZ).max(LIMITS.MAX_ELEMENT_IDS).optional(),
        background: z.string().max(LIMITS.MAX_COLOR_LENGTH).optional(),
        padding: z.number().min(0).max(500).finite().optional(),
    }, async ({ format, elementIds, background, padding }) => {
        try {
            const result = await client.exportScene(format, { elementIds, background, padding });
            if (format === 'svg') {
                return { content: [{ type: 'text', text: result.data }] };
            }
            return { content: [{ type: 'text', text: JSON.stringify(result.data, null, 2) }] };
        }
        catch (err) {
            return { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true };
        }
    });
    // Connect via stdio
    const transport = new StdioServerTransport();
    await server.connect(transport);
    logger.info({ mode, tools: 16 }, 'MCP server connected via stdio');
}
main().catch(err => {
    const logger2 = createLogger('mcp');
    logger2.fatal({ err }, 'MCP server failed to start');
    process.exit(1);
});
// Smithery sandbox server for capability scanning
export function createSandboxServer() {
    const server = new McpServer({
        name: 'excalidraw-mcp-server',
        version: '2.0.0',
    });
    const CoordZ = z.number().min(LIMITS.MIN_COORDINATE).max(LIMITS.MAX_COORDINATE).finite();
    const DimZ = z.number().min(0).max(LIMITS.MAX_DIMENSION).finite().optional();
    const ColorZ = z.string().max(LIMITS.MAX_COLOR_LENGTH).optional();
    const IdZ = z.string().max(LIMITS.MAX_ID_LENGTH);
    const IdsZ = z.array(IdZ).min(1).max(LIMITS.MAX_ELEMENT_IDS);
    const PointZ = z.object({ x: CoordZ, y: CoordZ });
    const elementFields = {
        type: z.enum(ELEMENT_TYPES),
        x: CoordZ, y: CoordZ,
        width: DimZ, height: DimZ,
        points: z.array(PointZ).max(LIMITS.MAX_POINTS).optional(),
        backgroundColor: ColorZ, strokeColor: ColorZ,
        strokeWidth: z.number().min(0).max(LIMITS.MAX_STROKE_WIDTH).finite().optional(),
        roughness: z.number().min(0).max(LIMITS.MAX_ROUGHNESS).finite().optional(),
        opacity: z.number().min(LIMITS.MIN_OPACITY).max(LIMITS.MAX_OPACITY).finite().optional(),
        text: z.string().max(LIMITS.MAX_TEXT_LENGTH).optional(),
        fontSize: z.number().min(1).max(LIMITS.MAX_FONT_SIZE).finite().optional(),
        fontFamily: z.number().int().min(1).max(4).optional(),
        groupIds: z.array(z.string().max(LIMITS.MAX_GROUP_ID_LENGTH)).max(LIMITS.MAX_GROUP_IDS).optional(),
        locked: z.boolean().optional(),
        angle: z.number().min(-360).max(360).finite().optional(),
    };
    const noop = async () => ({ content: [{ type: 'text', text: 'sandbox' }] });
    server.tool('create_element', 'Create a single Excalidraw element on the canvas', elementFields, noop);
    server.tool('update_element', 'Update an existing Excalidraw element by ID', { id: IdZ }, noop);
    server.tool('delete_element', 'Delete an Excalidraw element by ID', { id: IdZ }, noop);
    server.tool('query_elements', 'Search for elements by type, locked status, or group ID', { type: z.enum(ELEMENT_TYPES).optional(), locked: z.boolean().optional(), groupId: z.string().max(64).optional() }, noop);
    server.tool('get_resource', 'Get scene state, elements, theme, or library', { resource: z.enum(['scene', 'library', 'theme', 'elements']) }, noop);
    server.tool('batch_create_elements', `Create multiple elements at once (max ${LIMITS.MAX_BATCH_SIZE})`, { elements: z.array(z.object(elementFields)).min(1).max(LIMITS.MAX_BATCH_SIZE) }, noop);
    server.tool('group_elements', 'Group multiple elements together', { elementIds: z.array(IdZ).min(2).max(LIMITS.MAX_ELEMENT_IDS) }, noop);
    server.tool('ungroup_elements', 'Remove elements from a group by group ID', { groupId: z.string().max(64) }, noop);
    server.tool('align_elements', 'Align elements (left, center, right, top, middle, bottom)', { elementIds: z.array(IdZ).min(2).max(LIMITS.MAX_ELEMENT_IDS), alignment: z.enum(['left', 'center', 'right', 'top', 'middle', 'bottom']) }, noop);
    server.tool('distribute_elements', 'Distribute elements evenly (horizontal or vertical)', { elementIds: z.array(IdZ).min(3).max(LIMITS.MAX_ELEMENT_IDS), direction: z.enum(['horizontal', 'vertical']) }, noop);
    server.tool('lock_elements', 'Lock elements to prevent modification', { elementIds: IdsZ }, noop);
    server.tool('unlock_elements', 'Unlock elements to allow modification', { elementIds: IdsZ }, noop);
    server.tool('create_from_mermaid', 'Convert a Mermaid diagram to Excalidraw elements', { mermaidDiagram: z.string().min(1).max(LIMITS.MAX_MERMAID_LENGTH) }, noop);
    server.tool('export_scene', 'Export the canvas as PNG or SVG', { format: z.enum(['png', 'svg']), elementIds: z.array(IdZ).max(LIMITS.MAX_ELEMENT_IDS).optional(), background: ColorZ, padding: z.number().min(0).max(500).finite().optional() }, noop);
    server.tool('create_view', 'Render Excalidraw elements as an interactive inline diagram', { elements: z.array(z.object(elementFields)).min(1).max(LIMITS.MAX_BATCH_SIZE), title: z.string().max(200).optional(), background: ColorZ }, noop);
    server.tool('read_me', 'Get the Excalidraw element reference: types, colors, sizing, and tips', {}, noop);
    return server;
}
