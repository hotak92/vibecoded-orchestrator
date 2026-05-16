<script lang="ts">
  /**
   * /preferences/storage — wrapper route for the StorageSettingsCard.
   *
   * Stays tiny on purpose: the heavy lifting lives in
   * `$lib/components/StorageSettingsCard.svelte`, which can be embedded
   * elsewhere (e.g. the onboarding wizard's "Storage" step) without
   * forcing callers to navigate.
   */
  import StorageSettingsCard from '$lib/components/StorageSettingsCard.svelte';
  import Toast from '$lib/components/Toast.svelte';
</script>

<svelte:head>
  <title>Storage — VCT Launcher</title>
</svelte:head>

<div class="page">
  <nav class="crumb">
    <a href="/preferences">Preferences</a>
    <span class="sep">/</span>
    <span class="current">Storage</span>
  </nav>

  <StorageSettingsCard />

  <Toast />
</div>

<style>
  .page {
    padding: 1.5rem;
    max-width: 1000px;
    margin: 0 auto;
  }

  .crumb {
    margin-bottom: 1rem;
    font-size: 0.9rem;
    color: var(--text-muted, #666);
  }

  .crumb a {
    color: var(--accent, #2563eb);
    text-decoration: none;
  }

  .crumb a:hover {
    text-decoration: underline;
  }

  .crumb .sep {
    margin: 0 0.4rem;
    color: var(--border, #d4d4d8);
  }

  .crumb .current {
    color: var(--text, #1a1a1a);
    font-weight: 500;
  }
</style>
