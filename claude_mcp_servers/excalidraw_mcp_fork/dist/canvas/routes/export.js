import { Router } from 'express';
import { z } from 'zod';
import { createLogger } from '../../shared/logger.js';
const logger = createLogger('export');
const ExportBodySchema = z.object({
    format: z.enum(['png', 'svg']),
    elementIds: z.array(z.string().max(64)).max(500).optional(),
    background: z.string().max(32).optional(),
    padding: z.number().min(0).max(500).finite().optional(),
}).strict();
export function createExportRouter(store) {
    const router = Router();
    // POST /api/export - export scene as SVG (PNG requires headless browser)
    router.post('/', async (req, res) => {
        try {
            const result = ExportBodySchema.safeParse(req.body);
            if (!result.success) {
                res.status(400).json({
                    success: false,
                    error: 'Validation failed',
                    details: result.error.issues,
                });
                return;
            }
            const { format, elementIds, background, padding } = result.data;
            let elements = await store.getAll();
            if (elementIds && elementIds.length > 0) {
                const idSet = new Set(elementIds);
                elements = elements.filter(e => idSet.has(e.id));
            }
            if (elements.length === 0) {
                res.status(400).json({
                    success: false,
                    error: 'No elements to export',
                });
                return;
            }
            if (format === 'svg') {
                const svg = generateSvg(elements, background ?? '#ffffff', padding ?? 20);
                res.setHeader('Content-Type', 'image/svg+xml');
                res.setHeader('Content-Disposition', 'attachment; filename="excalidraw-export.svg"');
                res.send(svg);
                return;
            }
            // PNG export returns element data for client-side rendering
            // Full PNG export requires headless browser or canvas API on client
            res.json({
                success: true,
                format: 'png',
                message: 'PNG export data provided. Render on client using @excalidraw/utils.',
                elements,
                exportConfig: {
                    background: background ?? '#ffffff',
                    padding: padding ?? 20,
                },
            });
        }
        catch (err) {
            logger.error({ err }, 'Failed to export');
            res.status(500).json({ success: false, error: 'Internal server error' });
        }
    });
    return router;
}
function generateSvg(elements, background, padding) {
    // Calculate bounding box (account for arrow/line points)
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const el of elements) {
        if (el.points && el.points.length > 0) {
            for (const p of el.points) {
                minX = Math.min(minX, el.x + p.x);
                minY = Math.min(minY, el.y + p.y);
                maxX = Math.max(maxX, el.x + p.x);
                maxY = Math.max(maxY, el.y + p.y);
            }
        }
        else {
            const w = el.width ?? 100;
            const h = el.height ?? (el.type === 'text' ? (el.fontSize ?? 16) * 1.4 : 100);
            minX = Math.min(minX, el.x);
            minY = Math.min(minY, el.y);
            maxX = Math.max(maxX, el.x + w);
            maxY = Math.max(maxY, el.y + h);
        }
    }
    const width = maxX - minX + padding * 2;
    const height = maxY - minY + padding * 2;
    const offsetX = -minX + padding;
    const offsetY = -minY + padding;
    // Collect unique arrow stroke colors for per-color markers
    const arrowColors = new Set();
    for (const el of elements) {
        if (el.type === 'arrow') {
            arrowColors.add(el.strokeColor ?? '#000000');
        }
    }
    const svgElements = [];
    for (const el of elements) {
        const x = el.x + offsetX;
        const y = el.y + offsetY;
        const w = el.width ?? 100;
        const h = el.height ?? 100;
        const stroke = escapeXml(el.strokeColor ?? '#000000');
        const fill = escapeXml(el.backgroundColor ?? 'transparent');
        const sw = el.strokeWidth ?? 1;
        const opacity = (el.opacity ?? 100) / 100;
        switch (el.type) {
            case 'rectangle':
                svgElements.push(`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="8" ` +
                    `stroke="${stroke}" fill="${fill}" stroke-width="${sw}" opacity="${opacity}" />`);
                if (el.text) {
                    const fs = el.fontSize ?? 14;
                    svgElements.push(`<text x="${x + w / 2}" y="${y + h / 2}" font-size="${fs}" fill="${stroke}" ` +
                        `text-anchor="middle" dominant-baseline="central" opacity="${opacity}">${escapeXml(el.text)}</text>`);
                }
                break;
            case 'ellipse':
                svgElements.push(`<ellipse cx="${x + w / 2}" cy="${y + h / 2}" rx="${w / 2}" ry="${h / 2}" ` +
                    `stroke="${stroke}" fill="${fill}" stroke-width="${sw}" opacity="${opacity}" />`);
                if (el.text) {
                    const fs = el.fontSize ?? 14;
                    svgElements.push(`<text x="${x + w / 2}" y="${y + h / 2}" font-size="${fs}" fill="${stroke}" ` +
                        `text-anchor="middle" dominant-baseline="central" opacity="${opacity}">${escapeXml(el.text)}</text>`);
                }
                break;
            case 'diamond': {
                const cx = x + w / 2, cy = y + h / 2;
                const pts = `${cx},${y} ${x + w},${cy} ${cx},${y + h} ${x},${cy}`;
                svgElements.push(`<polygon points="${pts}" stroke="${stroke}" fill="${fill}" ` +
                    `stroke-width="${sw}" opacity="${opacity}" />`);
                if (el.text) {
                    const fs = el.fontSize ?? 14;
                    svgElements.push(`<text x="${cx}" y="${cy}" font-size="${fs}" fill="${stroke}" ` +
                        `text-anchor="middle" dominant-baseline="central" opacity="${opacity}">${escapeXml(el.text)}</text>`);
                }
                break;
            }
            case 'text': {
                const fs = el.fontSize ?? 16;
                const lines = (el.text ?? '').split('\n');
                const tspans = lines
                    .map((line, i) => `<tspan x="${x}" dy="${i === 0 ? 0 : fs * 1.2}">${escapeXml(line)}</tspan>`)
                    .join('');
                svgElements.push(`<text x="${x}" y="${y + fs}" ` +
                    `font-size="${fs}" fill="${stroke}" opacity="${opacity}">` +
                    `${tspans}</text>`);
                break;
            }
            case 'line':
            case 'arrow':
            case 'freedraw': {
                let d;
                if (el.points && el.points.length > 0) {
                    d = el.points
                        .map((p, i) => `${i === 0 ? 'M' : 'L'} ${x + p.x} ${y + p.y}`)
                        .join(' ');
                }
                else {
                    d = `M ${x} ${y} L ${x + w} ${y + h}`;
                }
                const markerId = el.type === 'arrow' ? colorToMarkerId(el.strokeColor ?? '#000000') : '';
                const marker = markerId ? ` marker-end="url(#${markerId})"` : '';
                svgElements.push(`<path d="${d}" stroke="${stroke}" fill="none" ` +
                    `stroke-width="${sw}" opacity="${opacity}"${marker} />`);
                break;
            }
        }
    }
    // Build per-color arrow markers
    const markers = [];
    for (const color of arrowColors) {
        const id = colorToMarkerId(color);
        markers.push(`    <marker id="${id}" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">`, `      <polygon points="0 0, 10 3.5, 0 7" fill="${escapeXml(color)}" />`, `    </marker>`);
    }
    return [
        `<?xml version="1.0" encoding="UTF-8"?>`,
        `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
        `  <defs>`,
        ...markers,
        `    <style>text { font-family: "Segoe UI", system-ui, -apple-system, sans-serif; }</style>`,
        `  </defs>`,
        `  <rect width="100%" height="100%" fill="${escapeXml(background)}" />`,
        `  ${svgElements.join('\n  ')}`,
        `</svg>`,
    ].join('\n');
}
function colorToMarkerId(color) {
    return 'arrow-' + color.replace(/[^a-zA-Z0-9]/g, '');
}
function escapeXml(str) {
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&apos;');
}
