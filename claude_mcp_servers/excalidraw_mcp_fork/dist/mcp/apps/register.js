import { registerAppTool, registerAppResource, RESOURCE_MIME_TYPE, } from '@modelcontextprotocol/ext-apps/server';
import { CREATE_VIEW_SCHEMA, handleCreateView } from '../tools/create-view.js';
import { handleReadMe } from '../tools/read-me.js';
import { createLogger } from '../../shared/logger.js';
const logger = createLogger('mcp-apps');
const WIDGET_RESOURCE_URI = 'ui://excalidraw/canvas';
/**
 * Register MCP Apps tools and resource on the server.
 *
 * - create_view: renders elements as an inline streaming widget
 * - read_me: returns the element reference cheatsheet
 * - ui://excalidraw/canvas resource: serves the compiled widget HTML
 */
export function registerMcpApps(server, opts) {
    // Register the create_view tool with MCP Apps UI metadata
    registerAppTool(server, 'create_view', {
        title: 'Create Excalidraw View',
        description: 'Render Excalidraw elements as an interactive inline diagram. ' +
            'Pass an array of elements with type, x, y coordinates and optional styling. ' +
            'The diagram streams in progressively as elements are generated. ' +
            'Use read_me first to see available element types and color palettes.',
        inputSchema: CREATE_VIEW_SCHEMA,
        _meta: {
            ui: { resourceUri: WIDGET_RESOURCE_URI },
        },
    }, async (args) => {
        try {
            return await handleCreateView(args, opts.persistToStore);
        }
        catch (err) {
            return {
                content: [{ type: 'text', text: `Error: ${err.message}` }],
                isError: true,
            };
        }
    });
    // Register read_me as a regular tool (no UI needed)
    server.tool('read_me', 'Get the Excalidraw element reference: types, colors, sizing, and tips. Call this before creating diagrams.', {}, async () => {
        try {
            return await handleReadMe();
        }
        catch (err) {
            return {
                content: [{ type: 'text', text: `Error: ${err.message}` }],
                isError: true,
            };
        }
    });
    // Register the widget HTML as an MCP Apps resource
    registerAppResource(server, 'Excalidraw Canvas', WIDGET_RESOURCE_URI, {
        description: 'Interactive Excalidraw canvas widget for inline diagram rendering',
    }, async () => {
        const html = await opts.getWidgetHtml();
        logger.debug('Serving widget HTML (%d bytes)', html.length);
        return {
            contents: [{
                    uri: WIDGET_RESOURCE_URI,
                    mimeType: RESOURCE_MIME_TYPE,
                    text: html,
                }],
        };
    });
    logger.info('MCP Apps tools registered (create_view, read_me)');
}
