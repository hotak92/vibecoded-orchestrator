/**
 * Partial JSON parser for streaming MCP Apps tool input.
 *
 * During streaming, the host sends healed JSON where unclosed brackets
 * are auto-closed. The last element in an array may be truncated.
 * This parser extracts only the fully complete elements from
 * a partial elements array, discarding the potentially incomplete tail.
 */
interface PartialElement {
    type?: string;
    x?: number;
    y?: number;
    [key: string]: unknown;
}
/**
 * Parse a partial JSON tool input and extract complete elements.
 * The host heals the JSON by closing brackets, so we get valid JSON
 * but the last element in the array might be incomplete.
 *
 * Strategy: parse the healed JSON, validate each element has at minimum
 * type + x + y, and drop any that don't. The last element is always
 * suspect during streaming, so we mark it separately.
 */
export declare function extractCompleteElements(partialArgs: Record<string, unknown> | null): {
    complete: PartialElement[];
    hasMore: boolean;
};
/**
 * Given the final (non-partial) tool input, extract all elements.
 * No need to worry about truncation here.
 */
export declare function extractFinalElements(args: Record<string, unknown>): PartialElement[];
/**
 * Compute which elements are new compared to a previous set.
 * Uses a simple index-based approach since elements arrive in order
 * during streaming.
 */
export declare function diffElements(previous: PartialElement[], current: PartialElement[]): PartialElement[];
export {};
//# sourceMappingURL=partial-json.d.ts.map