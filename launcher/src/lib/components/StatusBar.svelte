<script lang="ts">
  import { currentUser } from '$lib/stores/auth';

  let appCount = $derived($currentUser?.apps?.length ?? 0);

  // v0.2.43 (contributor branch feat/launcher-logo-circular-white):
  // version string moved to the right-sidebar brand footer
  // (RightSidebar.svelte `.rs-brand-footer`) so the statusbar
  // is no longer a duplicate display surface.
  // v0.2.91 (P2-B1): the "Connected" dot was never wired to any
  // real connectivity signal (no Weaviate/Ollama/hub check backed
  // it) and always rendered, even when those services were down.
  // Removed rather than wired to an unverified claim — StatusBar
  // now shows only the bound app-count.
</script>

<footer class="status-bar">
  <div class="status-right">
    <span>{appCount} app{appCount !== 1 ? 's' : ''} activated</span>
  </div>
</footer>

<style>
  .status-bar {
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 16px;
    background: rgba(5, 11, 31, 0.9);
    border-top: 1px solid rgba(255, 255, 255, 0.04);
    flex-shrink: 0;
    font-size: 11px;
    color: var(--color-muted);
  }

  .status-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }
</style>
