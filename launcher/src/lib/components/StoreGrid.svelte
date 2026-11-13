<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // The Store page's card grid (presentational: no store or Tauri imports,
  // so a test can render it). WHAT it lists is decided in
  // `$lib/store-catalog` (`storeEntries`); this file only draws it:
  //   * `coming_soon` → a "Coming soon" badge, and no action at all;
  //   * `catalog`     → a paid L0 module; its one action opens the Modules
  //                     page, where install and licensing live.
  import type { StoreEntry } from '$lib/store-catalog';

  let {
    entries,
    selectedId = null,
    rgbFor,
    onSelect,
    onOpen,
  }: {
    entries: StoreEntry[];
    selectedId?: string | null;
    /** RGB triplet ("r, g, b") of a brand color, for the icon wash. */
    rgbFor: (color: StoreEntry['color']) => string;
    onSelect: (entry: StoreEntry) => void;
    onOpen: (entry: StoreEntry) => void;
  } = $props();

  function colorVar(color: StoreEntry['color']): string {
    return `var(--color-${color})`;
  }
</script>

<div class="app-grid">
  {#each entries as entry (entry.id)}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="app-card glass-card"
      class:app-card-selected={selectedId === entry.id}
      data-store-id={entry.id}
      data-availability={entry.availability}
      onclick={() => onSelect(entry)}
      onkeydown={(e) => { if (e.key === 'Enter') onSelect(entry); }}
      role="button"
      tabindex="0"
    >
      <div class="app-card-top-line" style:background="linear-gradient(90deg, transparent, {colorVar(entry.color)}, transparent)"></div>
      <div class="app-card-icon" style:background="rgba({rgbFor(entry.color)}, 0.12)" style:border-color="rgba({rgbFor(entry.color)}, 0.25)">
        <span style:color={colorVar(entry.color)}>{entry.icon}</span>
      </div>
      <h3 class="app-card-name">{entry.name}</h3>
      <p class="app-card-desc">{entry.desc}</p>
      <div class="app-card-footer">
        <span class="app-card-version">{entry.version ? `v${entry.version}` : ''}</span>
        {#if entry.availability === 'coming_soon'}
          <span class="app-card-status app-card-soon">Coming soon</span>
        {:else}
          <button class="btn-3d btn-3d-ghost btn-3d-sm" onclick={(e) => { e.stopPropagation(); onOpen(entry); }}>
            View in Modules
          </button>
        {/if}
      </div>
    </div>
  {/each}
</div>

<style>
  .app-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 18px;
  }
  .app-card {
    position: relative;
    text-align: left;
    padding: 22px;
    cursor: pointer;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.06);
    font-family: inherit;
    color: inherit;
  }
  .app-card-selected {
    border-color: rgba(0, 191, 166, 0.3) !important;
    box-shadow:
      0 0 30px rgba(0, 191, 166, 0.08),
      0 8px 32px rgba(0, 0, 0, 0.2),
      inset 0 1px 0 rgba(255, 255, 255, 0.06) !important;
  }
  .app-card-top-line {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    opacity: 0;
    transition: opacity 0.3s ease;
  }
  .app-card:hover .app-card-top-line {
    opacity: 1;
  }
  .app-card-icon {
    width: 48px;
    height: 48px;
    border-radius: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px solid;
    margin-bottom: 16px;
    transition: transform 0.3s ease, box-shadow 0.3s ease;
  }
  .app-card-icon span {
    font-size: 20px;
    font-weight: 800;
  }
  .app-card-name {
    font-size: 15px;
    font-weight: 800;
    color: var(--color-text);
    margin-bottom: 6px;
  }
  .app-card-desc {
    font-size: 12px;
    color: var(--color-mid);
    line-height: 1.5;
    margin-bottom: 16px;
  }
  .app-card-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .app-card-version {
    font-size: 11px;
    color: var(--color-muted);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }
  .app-card-status {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 10px;
    border-radius: 20px;
  }
  .app-card-soon {
    color: var(--color-pink, #ff4fa0);
    background: rgba(255, 79, 160, 0.1);
  }
</style>
