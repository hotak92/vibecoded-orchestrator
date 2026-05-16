<script lang="ts">
  // Shared keychain manager (PR-4 of v0.2.11).
  //
  // Mounts <SecretsPanel /> — a Svelte component that shipped in PR #221
  // (v0.2.7) but was never wired into a navigable route. The panel is
  // self-contained: it ships its own three-tab scope toggle
  // (Per-project / Shared / Global) and writes to the OS keychain via
  // the `set_secret_v2` / `clear_secret_v2` / `list_user_secret_keys_v2`
  // Tauri commands. Toast is rendered globally by `+layout.svelte`, so
  // we don't double-mount it here.

  import { goto } from '$app/navigation';
  import SecretsPanel from '$lib/components/SecretsPanel.svelte';
</script>

<svelte:head>
  <title>Secrets — VCT Launcher</title>
</svelte:head>

<div class="sec-page">
  <header class="sec-header">
    <button class="sec-back" onclick={() => goto('/preferences')}>← Back</button>
    <h1>Secrets</h1>
  </header>

  <main class="sec-main">
    <section class="sec-intro">
      <p class="sec-hint">
        Keychain entries used across your projects. Values live in the OS keychain
        (macOS Keychain Access, Windows Credential Manager, libsecret on Linux) —
        never in plain files. Three scopes are available: per-project, shared
        (this user), and global (this machine).
      </p>
      <a class="sec-action-link" href="/preferences/secrets/import">
        Import existing secrets from <code>~/.vct-secrets/</code> →
      </a>
    </section>

    <section class="sec-panel">
      <SecretsPanel />
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
  .sec-action-link {
    font-size: 12px;
    color: var(--color-teal, rgb(0, 191, 166));
    text-decoration: none;
    align-self: flex-start;
  }
  .sec-action-link:hover {
    text-decoration: underline;
  }
  .sec-action-link code {
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    color: inherit;
  }
  .sec-panel {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    padding: 16px;
  }
</style>
