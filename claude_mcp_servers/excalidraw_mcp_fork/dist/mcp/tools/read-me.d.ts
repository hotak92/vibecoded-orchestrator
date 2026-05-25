/**
 * Handle the read_me tool call.
 * Returns the Excalidraw element reference cheatsheet so the LLM
 * knows element types, color palettes, sizing rules, and best practices
 * without needing separate documentation lookups.
 */
export declare function handleReadMe(): Promise<{
    content: Array<{
        type: 'text';
        text: string;
    }>;
}>;
//# sourceMappingURL=read-me.d.ts.map