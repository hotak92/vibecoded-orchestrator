<script lang="ts">
  // Floating filter panel for graph view (addendum: tag chips, node-type
  // chips, status chips, depth slider, "shared only" toggle).

  let {
    tags = [],
    types = [],
    statuses = [],
    selectedTags = $bindable<string[]>([]),
    selectedTypes = $bindable<string[]>([]),
    selectedStatuses = $bindable<string[]>([]),
    depthLimit = $bindable<number>(2),
    sharedOnly = $bindable<boolean>(false),
    nodeLimit = $bindable<number>(500),
  }: {
    tags?: string[];
    types?: string[];
    statuses?: string[];
    selectedTags?: string[];
    selectedTypes?: string[];
    selectedStatuses?: string[];
    depthLimit?: number;
    sharedOnly?: boolean;
    nodeLimit?: number;
  } = $props();

  function toggleChip(arr: string[], val: string): string[] {
    return arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val];
  }
</script>

<aside class="gfp">
  <h4>Filters</h4>

  {#if tags.length > 0}
    <div class="gfp-section">
      <span class="gfp-label">Tags</span>
      <div class="gfp-chips">
        {#each tags as t}
          <button
            class="gfp-chip"
            class:active={selectedTags.includes(t)}
            onclick={() => (selectedTags = toggleChip(selectedTags, t))}
          >{t}</button>
        {/each}
      </div>
    </div>
  {/if}

  {#if types.length > 0}
    <div class="gfp-section">
      <span class="gfp-label">Node type</span>
      <div class="gfp-chips">
        {#each types as t}
          <button
            class="gfp-chip"
            class:active={selectedTypes.includes(t)}
            onclick={() => (selectedTypes = toggleChip(selectedTypes, t))}
          >{t}</button>
        {/each}
      </div>
    </div>
  {/if}

  {#if statuses.length > 0}
    <div class="gfp-section">
      <span class="gfp-label">Status</span>
      <div class="gfp-chips">
        {#each statuses as t}
          <button
            class="gfp-chip"
            class:active={selectedStatuses.includes(t)}
            onclick={() => (selectedStatuses = toggleChip(selectedStatuses, t))}
          >{t}</button>
        {/each}
      </div>
    </div>
  {/if}

  <div class="gfp-section">
    <span class="gfp-label">Depth limit: {depthLimit}</span>
    <input type="range" min="1" max="6" bind:value={depthLimit} />
  </div>

  <div class="gfp-section">
    <span class="gfp-label">Max nodes: {nodeLimit}</span>
    <input type="range" min="50" max="2000" step="50" bind:value={nodeLimit} />
  </div>

  <div class="gfp-section">
    <label class="gfp-toggle">
      <input type="checkbox" bind:checked={sharedOnly} />
      <span>Shared only</span>
    </label>
  </div>
</aside>

<style>
  .gfp {
    position: absolute; top: 12px; right: 12px;
    width: 240px; max-height: calc(100% - 24px);
    overflow-y: auto;
    background: rgba(20,20,28,0.95); backdrop-filter: blur(8px);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 8px; padding: 12px;
    z-index: 10;
    font-size: 12px;
  }
  .gfp h4 { font-size: 12px; margin: 0 0 10px; text-transform: uppercase; color: #888; letter-spacing: 0.5px; }
  .gfp-section { margin-bottom: 10px; }
  .gfp-label { display: block; color: #aaa; font-size: 11px; margin-bottom: 4px; }
  .gfp-chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .gfp-chip {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.1);
    color: #ccc;
    padding: 2px 8px; border-radius: 12px; cursor: pointer;
    font-size: 11px;
  }
  .gfp-chip:hover { background: rgba(255,255,255,0.1); }
  .gfp-chip.active {
    background: rgba(0,191,166,0.25);
    border-color: rgba(0,191,166,0.6);
    color: var(--color-teal);
  }
  .gfp input[type="range"] { width: 100%; }
  .gfp-toggle { display: flex; align-items: center; gap: 6px; cursor: pointer; }
</style>
