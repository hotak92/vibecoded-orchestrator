<script lang="ts">
  // Bug 33: admin license-issuance test placeholder. Real flow:
  //   1. Maintainer issues a test license via the LS dashboard
  //      (Admin variant, test-mode, complimentary)
  //   2. Maintainer pastes the key here, hits Verify
  //   3. We call the real validate-tier endpoint → server-side
  //      classification → returns tier=admin if variant is admin
  //
  // Backend hookup pending; for now this is a smoke-test placeholder.
</script>

<div class="page">
  <h1>License issuance test</h1>
  <p class="hint">
    Verify a Lemon Squeezy test license against the live validate-tier endpoint.
    Admin variant detection happens server-side via <code>LS_ADMIN_VARIANT_IDS</code> —
    no client-side bypass exists.
  </p>
  <p class="hint">
    Workflow: LS dashboard → Admin variant → Issue test license → paste here → Verify.
    Backend hookup pending.
  </p>
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
    margin: 0 0 12px;
    line-height: 1.5;
  }
  code {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    color: #c4b3ff;
    background: rgba(255, 255, 255, 0.04);
    padding: 1px 5px;
    border-radius: 3px;
  }
</style>
