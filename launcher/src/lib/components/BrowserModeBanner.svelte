<script lang="ts">
  // Non-modal banner shown when the app is loaded in a browser (vite dev /
  // preview / Playwright) instead of the Tauri desktop runtime. Most
  // `invoke()` paths fail there; this surfaces it once at the top so users
  // know what they're looking at and individual screens can no-op
  // gracefully.
  //
  // Mounted from the root layout. Dismissible per-session via the X.

  import { isTauriRuntime } from '$lib/tauri';

  let dismissed = $state(false);
  const inTauri = isTauriRuntime();
</script>

{#if !inTauri && !dismissed}
  <div class="banner" role="status">
    <span class="dot" aria-hidden="true"></span>
    <span class="text">
      Browser preview mode — desktop features (project install, system detection,
      secrets) require running the app via <code>npm run tauri:dev</code>.
    </span>
    <button class="close" onclick={() => (dismissed = true)} aria-label="Dismiss">
      ×
    </button>
  </div>
{/if}

<style>
  .banner {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 16px;
    background: rgba(255, 159, 64, 0.10);
    border-bottom: 1px solid rgba(255, 159, 64, 0.25);
    color: #ffb066;
    font-size: 12px;
    line-height: 1.4;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #ffb066;
    flex-shrink: 0;
    box-shadow: 0 0 8px rgba(255, 176, 102, 0.6);
  }
  .text {
    flex: 1;
  }
  code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    padding: 1px 5px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.06);
    color: #ffd0a8;
  }
  .close {
    background: none;
    border: none;
    color: #ffb066;
    font-size: 18px;
    line-height: 1;
    cursor: pointer;
    padding: 0 4px;
    border-radius: 6px;
  }
  .close:hover {
    background: rgba(255, 255, 255, 0.06);
  }
</style>
