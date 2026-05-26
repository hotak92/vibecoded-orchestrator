<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.35 Agent L: inline Mermaid editor for the DiagramsTab "Draw new"
  // flow. Side-by-side textarea + live-preview pane for `.mmd` editing.
  // The component is intentionally minimal — it reuses the same `mermaid`
  // npm dependency DiagramsTab already imports for preview rendering;
  // we just wrap it in an editor UI.
  //
  // Lifecycle / save contract:
  //   - Parent owns persistence. We expose `source` via `bind:source` so
  //     the parent can read the current text on Save without us needing
  //     to invoke Tauri. On the very first save, the parent registers
  //     the diagram + writes the file (auto-register flow). Subsequent
  //     saves just write.
  //   - We debounce-render the preview at 200ms after the last keystroke
  //     to keep typing snappy even on slow Mermaid renders. Errors from
  //     mermaid.render() are surfaced in a side panel rather than thrown.
  //
  // Why no embedded "Save" button in this component: the parent wires
  // Save / Save-as-new into its own toolbar so the Mermaid editor stays
  // composable (the same component is reused for both registered-edit
  // and draft-new flows).
  import { onMount, untrack } from 'svelte';

  let {
    source = $bindable(''),
    diagramName,
  }: {
    source?: string;
    diagramName?: string;
  } = $props();

  let previewSvg = $state<string>('');
  let previewError = $state<string | null>(null);
  let renderTimer: ReturnType<typeof setTimeout> | null = null;
  let mermaidPromise: Promise<typeof import('mermaid').default> | null = null;
  let renderToken = 0;

  async function ensureMermaid() {
    if (!mermaidPromise) {
      mermaidPromise = (async () => {
        const mod = await import('mermaid');
        mod.default.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          fontFamily: 'system-ui, -apple-system, sans-serif',
        });
        return mod.default;
      })();
    }
    return mermaidPromise;
  }

  async function renderNow() {
    const myToken = ++renderToken;
    if (!source.trim()) {
      previewSvg = '';
      previewError = null;
      return;
    }
    try {
      const mermaid = await ensureMermaid();
      if (myToken !== renderToken) return; // a newer render started
      const safeName = (diagramName ?? 'draft').replace(/[^a-z0-9-]/gi, '-');
      const { svg } = await mermaid.render(
        `mermaid-editor-${safeName}-${myToken}`,
        source,
      );
      if (myToken !== renderToken) return;
      previewSvg = svg;
      previewError = null;
    } catch (e) {
      previewSvg = '';
      previewError = e instanceof Error ? e.message : String(e);
    }
  }

  function scheduleRender() {
    if (renderTimer) clearTimeout(renderTimer);
    renderTimer = setTimeout(() => {
      void renderNow();
    }, 200);
  }

  onMount(() => {
    // Initial render — the source may already have content (when the
    // parent loads an existing diagram into the editor for edit-mode).
    void renderNow();
  });

  $effect(() => {
    // Re-render whenever source changes. `untrack` avoids re-firing when
    // we ourselves mutate `previewSvg`/`previewError`.
    const _ = source;
    untrack(() => scheduleRender());
  });
</script>

<div class="mermaid-editor">
  <div class="mermaid-editor-pane mermaid-editor-source">
    <label class="mermaid-editor-label" for="mermaid-source">
      Mermaid source
    </label>
    <textarea
      id="mermaid-source"
      class="mermaid-editor-textarea"
      bind:value={source}
      placeholder={'flowchart TD\n  A[Start] --> B{Decision}\n  B -->|Yes| C[Action]\n  B -->|No| D[Other]'}
      spellcheck="false"
      aria-label="Mermaid source code"
    ></textarea>
  </div>
  <div class="mermaid-editor-pane mermaid-editor-preview">
    <span class="mermaid-editor-label" aria-hidden="true">Live preview</span>
    {#if previewError}
      <pre class="mermaid-editor-error">{previewError}</pre>
    {:else if previewSvg}
      <!-- securityLevel='strict' sanitises Mermaid output; safe to inject. -->
      <div class="mermaid-editor-svg-host">
        {@html previewSvg}
      </div>
    {:else}
      <p class="mermaid-editor-empty">
        Start typing Mermaid source to see a preview.
      </p>
    {/if}
  </div>
</div>

<style>
  .mermaid-editor {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    height: 100%;
    min-height: 320px;
  }
  .mermaid-editor-pane {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-height: 0;
  }
  .mermaid-editor-label {
    font-size: 11px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .mermaid-editor-textarea {
    flex: 1;
    background: rgba(0, 0, 0, 0.3);
    border: 1px solid rgba(255, 255, 255, 0.10);
    color: #e8e8ee;
    padding: 8px;
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 12px;
    line-height: 1.5;
    resize: none;
    min-height: 280px;
  }
  .mermaid-editor-textarea:focus {
    outline: none;
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 2px rgba(0, 191, 166, 0.1);
  }
  .mermaid-editor-preview {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 4px;
    padding: 8px;
    overflow: auto;
  }
  .mermaid-editor-svg-host {
    display: flex;
    justify-content: center;
    padding: 8px;
  }
  .mermaid-editor-svg-host :global(svg) {
    max-width: 100%;
    height: auto;
  }
  .mermaid-editor-error {
    background: rgba(255, 82, 82, 0.10);
    color: #ff8a8a;
    padding: 8px;
    border-radius: 4px;
    white-space: pre-wrap;
    font-family: ui-monospace, monospace;
    font-size: 11px;
    margin: 0;
  }
  .mermaid-editor-empty {
    color: #888;
    font-size: 12px;
    padding: 24px;
    text-align: center;
    margin: 0;
  }
</style>
