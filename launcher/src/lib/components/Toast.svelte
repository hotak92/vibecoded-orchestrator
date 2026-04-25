<script lang="ts">
  import { toast } from '$lib/stores/toast';
</script>

<div class="toast-host" aria-live="polite">
  {#each $toast as t (t.id)}
    <div class="toast toast-{t.kind}" role="status">
      <span class="toast-msg">{t.message}</span>
      <button class="toast-x" onclick={() => toast.dismiss(t.id)} aria-label="Dismiss">×</button>
    </div>
  {/each}
</div>

<style>
  .toast-host {
    position: fixed; bottom: 16px; right: 16px;
    display: flex; flex-direction: column; gap: 8px;
    z-index: 2000; max-width: 380px;
  }
  .toast {
    display: flex; align-items: flex-start; gap: 8px;
    padding: 10px 12px; border-radius: 6px;
    background: #1a1a22; border: 1px solid rgba(255,255,255,0.1);
    color: #e8e8ee; font-size: 12px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    animation: toast-in 180ms ease-out;
  }
  .toast-success { border-color: rgba(0,191,166,0.5); }
  .toast-error { border-color: rgba(255,99,99,0.5); background: #2a1818; }
  .toast-info { border-color: rgba(123,95,255,0.4); }
  .toast-msg { flex: 1; line-height: 1.4; word-break: break-word; }
  .toast-x {
    background: none; border: none; color: #888; cursor: pointer;
    font-size: 16px; line-height: 1; padding: 0 2px;
  }
  .toast-x:hover { color: #fff; }
  @keyframes toast-in {
    from { transform: translateY(8px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }
</style>
