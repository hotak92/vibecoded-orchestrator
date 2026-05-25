/**
 * Partial JSON parser for streaming MCP Apps tool input.
 *
 * During streaming, the host sends healed JSON where unclosed brackets
 * are auto-closed. The last element in an array may be truncated.
 * This parser extracts only the fully complete elements from
 * a partial elements array, discarding the potentially incomplete tail.
 */
/**
 * Parse a partial JSON tool input and extract complete elements.
 * The host heals the JSON by closing brackets, so we get valid JSON
 * but the last element in the array might be incomplete.
 *
 * Strategy: parse the healed JSON, validate each element has at minimum
 * type + x + y, and drop any that don't. The last element is always
 * suspect during streaming, so we mark it separately.
 */
export function extractCompleteElements(partialArgs) {
    if (!partialArgs)
        return { complete: [], hasMore: false };
    const elements = partialArgs.elements;
    if (!Array.isArray(elements) || elements.length === 0) {
        return { complete: [], hasMore: false };
    }
    const valid = [];
    for (const el of elements) {
        if (isCompleteElement(el)) {
            valid.push(el);
        }
    }
    // During streaming, the last valid element might still be incomplete
    // in ways we can't detect (e.g. truncated text field). We keep it
    // but flag that there may be more coming.
    return { complete: valid, hasMore: true };
}
/**
 * Given the final (non-partial) tool input, extract all elements.
 * No need to worry about truncation here.
 */
export function extractFinalElements(args) {
    const elements = args.elements;
    if (!Array.isArray(elements))
        return [];
    return elements.filter(isCompleteElement);
}
function isCompleteElement(el) {
    if (typeof el !== 'object' || el === null)
        return false;
    const obj = el;
    return (typeof obj.type === 'string' &&
        typeof obj.x === 'number' &&
        typeof obj.y === 'number');
}
/**
 * Compute which elements are new compared to a previous set.
 * Uses a simple index-based approach since elements arrive in order
 * during streaming.
 */
export function diffElements(previous, current) {
    if (current.length <= previous.length)
        return [];
    return current.slice(previous.length);
}
