<script lang="ts">
  /**
   * v0.2.52 V52-AD — host-wide module defaults page.
   *
   * Surface for the GLOBAL toggle introduced by V52-AD. Sibling to
   * each project's per-project Modules panel; the two share the same
   * `module_settings` table and `module:enabled-for-project-changed`
   * event channel.
   *
   * Page chrome (back button + heading) mirrors the other
   * /preferences/* routes (retrieval, storage, weaviate-untracked) for
   * a consistent navigation pattern. The heavy lifting lives in
   * `GlobalModuleTogglesPanel.svelte` so the panel can be embedded in
   * an onboarding step later without forcing callers to navigate.
   */
  import { goto } from '$app/navigation';
  import Toast from '$lib/components/Toast.svelte';
  import GlobalModuleTogglesPanel from '$lib/components/GlobalModuleTogglesPanel.svelte';
</script>

<svelte:head>
  <title>Module defaults — VCT Launcher</title>
</svelte:head>

<div class="mod-page">
  <header class="mod-header">
    <button class="mod-back" onclick={() => goto('/preferences')}>← Back</button>
    <h1>Module defaults</h1>
  </header>

  <main class="mod-main">
    <GlobalModuleTogglesPanel />
  </main>
</div>

<Toast />

<style>
  .mod-page {
    min-height: 100vh;
    background: var(--color-bg, #0e0e16);
    color: var(--color-light, #e8e8ee);
  }
  .mod-header {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 1.2rem 1.5rem 0 1.5rem;
    max-width: 760px;
    margin: 0 auto;
  }
  .mod-header h1 {
    margin: 0;
    font-size: 1.4rem;
  }
  .mod-back {
    background: transparent;
    border: 1px solid var(--border, rgba(255, 255, 255, 0.12));
    color: var(--color-light, #e8e8ee);
    border-radius: 6px;
    padding: 0.4rem 0.8rem;
    font-size: 0.85rem;
    cursor: pointer;
  }
  .mod-back:hover {
    background: rgba(255, 255, 255, 0.06);
  }
  .mod-main {
    padding-top: 0.5rem;
  }
</style>
