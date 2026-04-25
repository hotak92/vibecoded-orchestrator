<script lang="ts">
  import { currentUser } from '$lib/stores/auth';

  let appCount = $derived($currentUser?.apps?.length ?? 0);
</script>

<footer class="status-bar">
  <div class="status-left">
    <div class="status-dot"></div>
    <span>Connected</span>
  </div>
  <div class="status-right">
    <span>{appCount} app{appCount !== 1 ? 's' : ''} activated</span>
    <span class="status-sep">|</span>
    <span>v0.1.0</span>
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

  .status-left {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--color-teal);
    box-shadow: 0 0 8px rgba(0, 191, 166, 0.6);
  }

  .status-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .status-sep {
    opacity: 0.3;
  }
</style>
