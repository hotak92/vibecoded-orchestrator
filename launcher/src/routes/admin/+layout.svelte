<script lang="ts">
  // Gate every /admin/* route on the cached "admin" tier.
  // The check is client-side for UX (show "Admin only" placeholder
  // instead of a 404), but every admin tab also re-checks tier
  // server-side before exposing data. Patching this layout to bypass
  // the check unlocks the placeholder text but not the data behind it.
  import { license } from '$lib/stores/license';
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();

  const isAdmin = $derived(($license.cache?.orchestrator_tier ?? 'free') === 'admin');
</script>

{#if isAdmin}
  {@render children()}
{:else}
  <div class="admin-deny">
    <h2>Admin only</h2>
    <p>
      This route is reserved for users with an Admin orchestrator license. Server-side
      tier classification gates the data behind it; client-side bypass gives you nothing.
    </p>
    <p>
      If you should have access, run <code>/license_refresh</code> from the Settings panel
      and check that <code>orchestrator_tier</code> in the license cache reads
      <code>admin</code>.
    </p>
  </div>
{/if}

<style>
  .admin-deny {
    margin: 40px auto;
    max-width: 640px;
    padding: 24px 28px;
    border: 1px solid rgba(255, 184, 74, 0.3);
    background: rgba(255, 184, 74, 0.04);
    border-radius: 8px;
    color: #ddd;
  }
  .admin-deny h2 {
    margin: 0 0 12px;
    color: #ffb84a;
    font-size: 18px;
  }
  .admin-deny p {
    line-height: 1.5;
    font-size: 13px;
  }
  .admin-deny code {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    color: #c4b3ff;
    background: rgba(255, 255, 255, 0.04);
    padding: 1px 5px;
    border-radius: 3px;
  }
</style>
