<script lang="ts">
  // The Store (owner ruling 2026-11-12): the announced products Transcrypt and
  // VCT Coordination, both "Coming soon", plus any paid module the L0 catalog
  // lists later. The list is decided in `$lib/store-catalog`; the cards are
  // drawn by `StoreGrid`. Free and bundled modules (the RL reranker included)
  // are managed from the Modules page, not sold here.
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import RightSidebar from '$lib/components/RightSidebar.svelte';
  import StoreGrid from '$lib/components/StoreGrid.svelte';
  import { getColorRgb } from '$lib/color-rgb';
  import { modules } from '$lib/stores/modules';
  import { ui } from '$lib/stores/ui';
  import { storeEntries, type StoreEntry } from '$lib/store-catalog';

  onMount(() => {
    // The catalog-fetch machinery stays: a paid module published to L0
    // renders here without a launcher change.
    modules.loadCatalog();
  });

  const entries = $derived(storeEntries($modules.catalog));

  let selected = $state<StoreEntry | null>(null);

  function selectEntry(entry: StoreEntry) {
    selected = selected?.id === entry.id ? null : entry;
  }

  function openInModules() {
    void goto('/modules');
  }

  // The right rail's view of the selection: a coming-soon product carries the
  // explicit stage (no install / launch / update actions); a catalog module
  // carries its catalog `kind`, the rail's authoritative status signal.
  const sidebarApp = $derived(
    selected
      ? {
          id: selected.id,
          name: selected.name,
          color: selected.color,
          version: selected.version,
          catalogKind: selected.catalogKind,
          ...(selected.availability === 'coming_soon' ? { stage: 'coming_soon' as const } : {}),
        }
      : null,
  );
</script>

<svelte:head>
  <title>Store — VCT Launcher</title>
</svelte:head>

<div class="page">
  <div class="content">
    <div class="main-inner">
      <div class="content-header">
        <div>
          <h1 class="content-title">Store</h1>
          <p class="content-subtitle">Discover VibeCoded Tools</p>
        </div>
      </div>

      <StoreGrid
        {entries}
        selectedId={selected?.id ?? null}
        rgbFor={getColorRgb}
        onSelect={selectEntry}
        onOpen={openInModules}
      />
    </div>
  </div>

  <RightSidebar selectedApp={sidebarApp} onOpenActivation={() => ui.openActivation()} />
</div>

<style>
  .page {
    display: flex;
    height: 100%;
    overflow: hidden;
  }
  .content {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
  }
  .main-inner {
    padding: 28px 32px;
  }
  .content-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
  }
  .content-title {
    font-size: 22px;
    font-weight: 800;
    color: var(--color-text);
    letter-spacing: -0.5px;
  }
  .content-subtitle {
    font-size: 13px;
    color: var(--color-mid);
    margin-top: 2px;
  }
</style>
