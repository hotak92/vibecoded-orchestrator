<script lang="ts">
  // Bulk-import existing on-disk secrets (PR-4 of v0.2.11).
  //
  // Mounts <SecretsImportPanel /> — shipped in PR #223 (v0.2.8) but
  // never wired into a navigable route. The panel reads importable
  // KEY+source pairs via `list_importable_secret_keys` and registers
  // selected entries via `register_secret_from_source`. Value-handling
  // rule (INVIOLABLE): the frontend only holds the KEY and the source
  // descriptor — the backend reads each value off disk itself.

  import { goto } from '$app/navigation';
  import SecretsImportPanel from '$lib/components/SecretsImportPanel.svelte';
</script>

<svelte:head>
  <title>Import secrets — VCT Launcher</title>
</svelte:head>

<div class="sec-page">
  <header class="sec-header">
    <button class="sec-back" onclick={() => goto('/preferences/secrets')}>← Back to Secrets</button>
    <h1>Import existing secrets</h1>
  </header>

  <main class="sec-main">
    <section class="sec-intro">
      <p class="sec-hint">
        One-shot migration from the on-disk <code>~/.vct-secrets/shared/</code>
        store and project <code>.env</code> files into the launcher's OS keychain.
        After import, the original files are left untouched — delete them manually
        once you've verified the keychain entries work.
      </p>
      <p class="sec-hint sec-hint-warn">
        Privacy note: the launcher never reads secret values into the UI; only
        keys and source paths are surfaced. Imports happen entirely backend-side.
      </p>
    </section>

    <section class="sec-panel">
      <SecretsImportPanel />
    </section>
  </main>
</div>

<style>
  .sec-page {
    min-height: 100vh;
    background: var(--color-bg, #0e0e16);
    color: var(--color-light, #e8e8ee);
  }
  .sec-header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 24px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }
  .sec-header h1 {
    font-size: 16px;
    margin: 0;
  }
  .sec-back {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.1);
    color: inherit;
    padding: 4px 10px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .sec-main {
    max-width: 880px;
    margin: 0 auto;
    padding: 16px;
  }
  .sec-intro {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    padding: 14px;
    margin-bottom: 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .sec-hint {
    font-size: 12px;
    color: var(--color-mid, #888);
    line-height: 1.55;
    margin: 0;
  }
  .sec-hint code {
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
  }
  .sec-hint-warn {
    color: rgb(255, 200, 120);
  }
  .sec-panel {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    padding: 16px;
  }
</style>
