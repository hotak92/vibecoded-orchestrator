import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
/**
 * Register MCP Apps tools and resource on the server.
 *
 * - create_view: renders elements as an inline streaming widget
 * - read_me: returns the element reference cheatsheet
 * - ui://excalidraw/canvas resource: serves the compiled widget HTML
 */
export declare function registerMcpApps(server: McpServer, opts: {
    getWidgetHtml: () => Promise<string>;
    persistToStore?: (elements: Record<string, unknown>[]) => Promise<void>;
}): void;
//# sourceMappingURL=register.d.ts.map