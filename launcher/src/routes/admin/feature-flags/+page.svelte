<script lang="ts">
  // Bug 33: admin feature-flags placeholder. Wired to no real backend
  // yet — admins can still see the UI shape so we know the route works.
  // Future: toggle pre-release modules + dev features here. The tier
  // gate above this route enforces access; this page just renders.
</script>

<div class="page">
  <h1>Feature flags</h1>
  <p class="hint">
    Admin-only toggles for pre-release modules and dev affordances.
    Backend implementation pending.
  </p>
  <ul class="flags">
    <li>
      <span class="flag-name">Pre-release modules in catalog</span>
      <span class="flag-status">visible (admin tier unlocks all)</span>
    </li>
    <li>
      <span class="flag-name">Dev console in MCP dashboard</span>
      <span class="flag-status">visible (admin tier)</span>
    </li>
    <li>
      <span class="flag-name">License issuance test routes</span>
      <span class="flag-status">visible (admin tier)</span>
    </li>
  </ul>
</div>

<style>
  .page {
    padding: 24px;
  }
  h1 {
    margin: 0 0 8px;
    font-size: 20px;
  }
  .hint {
    color: var(--color-muted);
    font-size: 13px;
    margin: 0 0 16px;
  }
  .flags {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .flags li {
    display: flex;
    justify-content: space-between;
    padding: 10px 12px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 6px;
    margin-bottom: 6px;
    font-size: 13px;
  }
  .flag-name {
    color: #ddd;
  }
  .flag-status {
    color: #ffb84a;
    font-family: ui-monospace, monospace;
    font-size: 11px;
  }
</style>
