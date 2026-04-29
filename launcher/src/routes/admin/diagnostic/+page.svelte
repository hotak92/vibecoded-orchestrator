<script lang="ts">
  // Admin diagnostic page — license + container + KG state inspector.
  // Read-only. Reuses existing Tauri commands so no additional
  // backend gates are needed.
  import { license } from '$lib/stores/license';
</script>

<div class="page">
  <h1>Diagnostics</h1>
  <p class="hint">License + cache state. Read-only inspector for admins.</p>
  <pre class="json">{JSON.stringify($license, null, 2)}</pre>
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
  .json {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    background: rgba(0, 0, 0, 0.3);
    padding: 12px;
    border-radius: 6px;
    color: #c4b3ff;
    overflow: auto;
    max-height: 60vh;
    white-space: pre-wrap;
    word-break: break-all;
  }
</style>
