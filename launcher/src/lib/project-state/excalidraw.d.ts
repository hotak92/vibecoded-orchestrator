// SPDX-License-Identifier: AGPL-3.0-or-later
// Type shims for the vendored Excalidraw fork + its peer React deps.
//
// We bundle Excalidraw via the published `@excalidraw/excalidraw` package
// (deps in launcher/package.json) but DO NOT install the heavy
// `@types/react` / `@types/react-dom` packages — they pull in the full
// React type universe (~10 MB of .d.ts) only to type three call sites
// in `ExcalidrawEditor.svelte`. The narrow shims below cover exactly
// what the editor uses; if Phase 2.x adds more React surface the shims
// can grow incrementally.
//
// If we ever switch off the embedded React component (e.g. wrap
// Excalidraw via a vanilla-JS API), delete this file.

declare module 'react' {
  export type ReactNode = unknown;
  export function createElement(type: unknown, props?: unknown, ...children: unknown[]): unknown;
  const React: {
    createElement: typeof createElement;
  };
  export default React;
}

declare module 'react-dom/client' {
  export interface Root {
    render(element: unknown): void;
    unmount(): void;
  }
  export function createRoot(container: Element | DocumentFragment): Root;
}

declare module '@excalidraw/excalidraw' {
  // Excalidraw's React component is consumed via React.createElement,
  // not JSX, so we only need the component reference + the API surface
  // ExcalidrawEditor.svelte actually touches.
  export const Excalidraw: unknown;

  export interface ExcalidrawImperativeAPI {
    getSceneElements(): unknown[];
    getAppState(): { name?: string; [k: string]: unknown };
    getFiles(): unknown;
    updateScene(scene: { elements?: unknown[]; appState?: unknown }): void;
    // SVG export — see https://docs.excalidraw.com/docs/@excalidraw/excalidraw/api/utils
    // exposed via `exportToSvg` import; not on the API but adjacent.
  }

  export function exportToSvg(opts: {
    elements: readonly unknown[];
    appState?: unknown;
    files?: unknown;
    exportPadding?: number;
  }): Promise<SVGSVGElement>;

  export function serializeAsJSON(
    elements: readonly unknown[],
    appState: unknown,
    files: unknown,
    type: 'local' | 'database',
  ): string;
}
