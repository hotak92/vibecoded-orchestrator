<script lang="ts">
  /**
   * Retrieval tuning route — wraps the standalone panel in the
   * standard Preferences page chrome (back button + heading).
   *
   * The panel itself (`RetrievalTuningPanel.svelte`) is decoupled
   * from the route so it can be mounted inline elsewhere later (e.g.
   * an Advanced section of a per-project Settings tab) without a
   * route move. Per the v0.2.22 plan we explicitly defer the
   * per-project override surface; this route is the global-only flow.
   */
  import { goto } from '$app/navigation';
  import Toast from '$lib/components/Toast.svelte';
  import RetrievalTuningPanel from '$lib/components/RetrievalTuningPanel.svelte';
</script>

<div class="rt-page">
  <header class="rt-page-header">
    <button class="rt-back" onclick={() => goto('/preferences')}>← Back</button>
    <h1>Retrieval tuning</h1>
  </header>

  <main class="rt-page-main">
    <RetrievalTuningPanel />
  </main>
</div>

<Toast />

<style>
  .rt-page {
    max-width: 880px;
    margin: 0 auto;
    padding: 1.5rem;
  }

  .rt-page-header {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .rt-page-header h1 {
    margin: 0;
    font-size: 1.4rem;
  }

  .rt-back {
    background: transparent;
    color: var(--text-2, #aaa);
    border: 1px solid var(--border-1, #2a2a2a);
    border-radius: 4px;
    padding: 0.3rem 0.7rem;
    cursor: pointer;
    font-size: 0.85rem;
  }

  .rt-back:hover {
    background: var(--surface-2, #232323);
  }

  /* Panel supplies its own surface/borders so .rt-page-main needs no
     additional rules; no selector emitted to avoid empty-ruleset warning. */
</style>
