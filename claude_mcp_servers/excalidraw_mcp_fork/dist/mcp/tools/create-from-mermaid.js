import { MermaidSchema } from '../schemas/element.js';
export async function createFromMermaidTool(args, client) {
    const { mermaidDiagram, config } = MermaidSchema.parse(args);
    await client.convertMermaid(mermaidDiagram, config);
    return { success: true, message: 'Mermaid conversion sent to canvas' };
}
