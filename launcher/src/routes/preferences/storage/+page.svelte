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

  /* `--text-muted`, `--border` and `--text` are defined nowhere in app.css,
     so these painted their light-theme fallbacks: the current crumb was
     #1a1a1a on the navy background (contrast 1.1:1). Real tokens now. */
  .crumb {
    margin-bottom: 1rem;
    font-size: 0.9rem;
    color: var(--color-mid);
  }

  .crumb a {
    color: var(--color-teal);
    text-decoration: none;
  }

  .crumb a:hover {
    text-decoration: underline;
  }

  .crumb .sep {
    margin: 0 0.4rem;
    color: var(--color-muted);
  }

  .crumb .current {
    color: var(--color-text);
    font-weight: 500;
  }
</style>
