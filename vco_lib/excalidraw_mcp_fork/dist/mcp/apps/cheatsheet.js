/**
 * Excalidraw element reference and color palettes for the read_me tool.
 * Gives the LLM a quick reference for building diagrams without
 * needing to discover conventions through trial and error.
 */
export const ELEMENT_REFERENCE = {
    types: {
        rectangle: {
            description: 'Rectangular shape. Use for boxes, containers, cards.',
            requiredFields: ['x', 'y'],
            optionalFields: ['width', 'height', 'backgroundColor', 'strokeColor', 'text'],
            defaults: { width: 200, height: 100 },
        },
        ellipse: {
            description: 'Oval/circle shape. Use for nodes, badges, indicators.',
            requiredFields: ['x', 'y'],
            optionalFields: ['width', 'height', 'backgroundColor', 'strokeColor', 'text'],
            defaults: { width: 150, height: 150 },
        },
        diamond: {
            description: 'Diamond/rhombus shape. Use for decision points in flowcharts.',
            requiredFields: ['x', 'y'],
            optionalFields: ['width', 'height', 'backgroundColor', 'strokeColor', 'text'],
            defaults: { width: 150, height: 150 },
        },
        arrow: {
            description: 'Directional arrow connecting points. Use for flows and relationships.',
            requiredFields: ['x', 'y', 'points'],
            optionalFields: ['strokeColor', 'strokeWidth'],
            example: { x: 0, y: 0, points: [{ x: 0, y: 0 }, { x: 200, y: 0 }] },
        },
        line: {
            description: 'Straight or polyline segment. Use for dividers, connections without direction.',
            requiredFields: ['x', 'y', 'points'],
            optionalFields: ['strokeColor', 'strokeWidth'],
        },
        text: {
            description: 'Standalone text label. Use for titles, annotations, labels.',
            requiredFields: ['x', 'y', 'text'],
            optionalFields: ['fontSize', 'fontFamily'],
            defaults: { fontSize: 20 },
        },
        freedraw: {
            description: 'Freehand drawing path. Use for sketchy annotations.',
            requiredFields: ['x', 'y', 'points'],
            optionalFields: ['strokeColor', 'strokeWidth'],
        },
    },
    colorPalettes: {
        excalidraw: {
            description: 'Default Excalidraw hand-drawn palette',
            colors: {
                blue: '#1971c2',
                red: '#c92a2a',
                green: '#2f9e44',
                orange: '#e67700',
                yellow: '#f59f00',
                purple: '#6741d9',
                pink: '#c2255c',
                gray: '#868e96',
                black: '#1b1b1f',
                white: '#ffffff',
            },
        },
        pastel: {
            description: 'Soft pastel backgrounds for containers and cards',
            colors: {
                lightBlue: '#d0ebff',
                lightRed: '#ffe3e3',
                lightGreen: '#d3f9d8',
                lightOrange: '#fff4e6',
                lightYellow: '#fff9db',
                lightPurple: '#e5dbff',
                lightPink: '#ffdeeb',
                lightGray: '#f1f3f5',
            },
        },
    },
    sizing: {
        spacing: 'Keep 40-60px between elements for readable layouts',
        textInBox: 'Add 20px padding around text inside rectangles',
        arrowGap: 'Start arrows 10px from source edge, end 10px before target',
        minWidth: 'Minimum readable element width is 80px',
        fontSize: {
            title: 28,
            heading: 22,
            body: 16,
            caption: 12,
        },
    },
    tips: [
        'Use batch_create_elements to place multiple elements at once - faster than individual creates',
        'Group related elements with group_elements after creating them',
        'Use align_elements and distribute_elements to clean up layouts',
        'For flowcharts: diamond = decision, rectangle = process, ellipse = start/end',
        'Set roughness: 0 for clean lines, 1 for hand-drawn look, 2 for sketchy',
        'Lock elements with lock_elements to prevent accidental changes to finished sections',
    ],
};
export function getCheatsheetText() {
    const ref = ELEMENT_REFERENCE;
    const lines = [];
    lines.push('# Excalidraw MCP Quick Reference\n');
    lines.push('## Element Types\n');
    for (const [name, info] of Object.entries(ref.types)) {
        lines.push(`### ${name}`);
        lines.push(info.description);
        lines.push(`Required: ${info.requiredFields.join(', ')}`);
        if (info.optionalFields) {
            lines.push(`Optional: ${info.optionalFields.join(', ')}`);
        }
        if ('defaults' in info && info.defaults) {
            lines.push(`Defaults: ${JSON.stringify(info.defaults)}`);
        }
        if ('example' in info && info.example) {
            lines.push(`Example: ${JSON.stringify(info.example)}`);
        }
        lines.push('');
    }
    lines.push('## Color Palettes\n');
    for (const [name, palette] of Object.entries(ref.colorPalettes)) {
        lines.push(`### ${name} - ${palette.description}`);
        for (const [colorName, hex] of Object.entries(palette.colors)) {
            lines.push(`  ${colorName}: ${hex}`);
        }
        lines.push('');
    }
    lines.push('## Sizing Guidelines\n');
    for (const [key, val] of Object.entries(ref.sizing)) {
        if (typeof val === 'string') {
            lines.push(`- ${key}: ${val}`);
        }
        else {
            lines.push(`- ${key}:`);
            for (const [k, v] of Object.entries(val)) {
                lines.push(`    ${k}: ${v}px`);
            }
        }
    }
    lines.push('');
    lines.push('## Tips\n');
    for (const tip of ref.tips) {
        lines.push(`- ${tip}`);
    }
    return lines.join('\n');
}
