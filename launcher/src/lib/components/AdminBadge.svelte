<script lang="ts">
  // Bug 33: persistent corner badge that appears whenever the cached
  // orchestrator tier is "admin". Color is amber/orange so it's
  // visible but not alarming — its purpose is to remind the user
  // they're running with elevated dev affordances on.
  //
  // The visibility check happens on the SAME tier string the rest of
  // the app uses (license_get_tier → cache.orchestrator_tier). Admin
  // is server-classified by validate-tier when the variant_id is in
  // LS_ADMIN_VARIANT_IDS — there is no local bypass.
  //
  // Public/release builds don't suppress this badge — its presence is
  // a visual reminder, not a debug-only marker. If the user has an
  // admin license activated, the badge shows.

  import { license } from '$lib/stores/license';

  const isAdmin = $derived(($license.cache?.orchestrator_tier ?? 'free') === 'admin');
  const expiresAt = $derived($license.cache?.orchestrator_tier === 'admin'
    ? null // expires_at field not currently in TierCacheView; future enhancement
    : null);
</script>

{#if isAdmin}
  <div
    class="admin-badge"
    title="License validation classified you as admin (server-side via LS_ADMIN_VARIANT_IDS). The badge is a visual reminder that elevated dev affordances are active."
    role="status"
    aria-label="Admin license active"
  >
    <span class="admin-badge-dot"></span>
    <span class="admin-badge-text">ADMIN</span>
    {#if expiresAt}
      <span class="admin-badge-expires">expires {expiresAt}</span>
    {/if}
  </div>
{/if}

<style>
  .admin-badge {
    position: fixed;
    bottom: 14px;
    right: 14px;
    z-index: 9999;
    display: inline-flex;
    gap: 6px;
    align-items: center;
    padding: 4px 10px;
    background: rgba(255, 184, 74, 0.14);
    border: 1px solid rgba(255, 184, 74, 0.5);
    border-radius: 12px;
    color: #ffb84a;
    font-family: ui-monospace, monospace;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    pointer-events: auto;
    user-select: none;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  }
  .admin-badge-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #ffb84a;
    box-shadow: 0 0 6px rgba(255, 184, 74, 0.8);
  }
  .admin-badge-text {
    font-size: 10px;
  }
  .admin-badge-expires {
    color: #d49a3e;
    font-weight: 500;
    font-size: 9px;
    margin-left: 4px;
  }
</style>
