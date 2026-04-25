<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke, safeInvoke, isTauriRuntime } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import type { TelemetryStatus, TelemetryEventView, ConsentFlags } from '$lib/types/project-state';

  let status = $state<TelemetryStatus | null>(null);
  let events = $state<TelemetryEventView[]>([]);
  let loading = $state(true);

  const inTauri = isTauriRuntime();

  async function loadStatus() {
    loading = true;
    try {
      status = await safeInvoke<TelemetryStatus>('telemetry_status');
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  async function loadEvents() {
    try {
      const result = await safeInvoke<TelemetryEventView[]>('telemetry_recent_events', { limit: 50 });
      events = result ?? [];
    } catch (e) {
      toast.error(e);
    }
  }

  async function setConsent(flag: keyof ConsentFlags, value: boolean) {
    if (!status) return;
    const next: ConsentFlags = { ...status.consent, [flag]: value };
    try {
      const updated = await invoke<ConsentFlags>('telemetry_set_consent', { flags: next });
      status = { ...status, consent: updated };
      toast.success('Saved');
    } catch (e) {
      toast.error(e);
    }
  }

  async function clearQueue() {
    if (!confirm('Clear queued telemetry events? They will be deleted before being sent.')) return;
    try {
      await invoke('telemetry_clear_queue');
      toast.success('Queue cleared');
      await Promise.all([loadStatus(), loadEvents()]);
    } catch (e) {
      toast.error(e);
    }
  }

  onMount(async () => {
    await Promise.all([loadStatus(), loadEvents()]);
  });
</script>

<div class="t-page">
  <header class="t-header">
    <button class="t-back" onclick={() => goto('/')}>← Back</button>
    <h1>Telemetry & Privacy</h1>
  </header>

  {#if !inTauri}
    <p class="t-empty t-placeholder">
      Telemetry settings require the desktop app. Run
      <code>npm run tauri:dev</code> from <code>launcher/</code> to view
      consent flags and queued events.
    </p>
  {:else if loading || !status}
    <p class="t-empty">Loading…</p>
  {:else}
    <main class="t-main">
      {#if status.disabled_via_env}
        <div class="t-warn">
          Telemetry is currently disabled by the <code>VIBECODED_TELEMETRY=false</code> env var.
          Local consent settings are ignored until you unset it.
        </div>
      {/if}

      <section class="t-section">
        <h2>Consent</h2>
        <p class="t-hint">
          The <em>always-on</em> channel is locked: it carries license validation pings + opaque error rates and
          cannot be disabled. Everything else below is opt-in.
        </p>

        <label class="t-row" title="Always-on: license + error rates. Cannot be disabled.">
          <input type="checkbox" checked={status.consent.always_on} disabled />
          <strong>Always-on (locked)</strong>
        </label>

        <label class="t-row">
          <input type="checkbox" checked={status.consent.rl_data}
            onchange={(e) => setConsent('rl_data', (e.target as HTMLInputElement).checked)} />
          <strong>RL training data</strong>
          <small>Anonymized routing decisions used to retrain the model.</small>
        </label>

        <label class="t-row">
          <input type="checkbox" checked={status.consent.routing_data}
            onchange={(e) => setConsent('routing_data', (e.target as HTMLInputElement).checked)} />
          <strong>Routing logs</strong>
          <small>Per-request model selection + latency.</small>
        </label>

        <label class="t-row">
          <input type="checkbox" checked={status.consent.instinct_data}
            onchange={(e) => setConsent('instinct_data', (e.target as HTMLInputElement).checked)} />
          <strong>Instinct pipeline</strong>
          <small>Tool-usage patterns for the instinct learner.</small>
        </label>

        <label class="t-row">
          <input type="checkbox" checked={status.consent.hardware}
            onchange={(e) => setConsent('hardware', (e.target as HTMLInputElement).checked)} />
          <strong>Hardware fingerprint</strong>
          <small>OS / CPU / RAM / GPU class — for compatibility stats.</small>
        </label>

        {#if status.consent.granted_at}
          <p class="t-meta">Last updated {status.consent.granted_at}</p>
        {/if}
      </section>

      <section class="t-section">
        <header class="t-section-h">
          <h2>Queue</h2>
          <div>
            <button class="t-btn" onclick={loadStatus}>Refresh</button>
            <button class="t-btn t-btn-warn" onclick={clearQueue} disabled={status.queue_size === 0}>
              Clear queue
            </button>
          </div>
        </header>
        <p class="t-meta">
          {status.queue_size} event(s) pending
          {#if status.last_upload_at}
            · last upload {new Date(status.last_upload_at * 1000).toLocaleString()}
          {/if}
        </p>
        {#if status.last_upload_error}
          <p class="t-warn-inline">Last upload error: {status.last_upload_error}</p>
        {/if}
      </section>

      <section class="t-section">
        <h2>Recent events</h2>
        {#if events.length === 0}
          <p class="t-empty">No events captured.</p>
        {:else}
          <table class="t-table">
            <thead><tr><th>Type</th><th>Created</th><th>Sent?</th><th>Payload</th></tr></thead>
            <tbody>
              {#each events as e}
                <tr>
                  <td><code>{e.event_type}</code></td>
                  <td><small>{new Date(e.created_at * 1000).toLocaleString()}</small></td>
                  <td>
                    {#if e.uploaded_at}<span class="t-ok">✓</span>
                    {:else}<span class="t-pending">queued</span>{/if}
                  </td>
                  <td><code class="t-payload">{e.payload_summary}</code></td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </section>
    </main>
  {/if}
</div>

<Toast />

<style>
  .t-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .t-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .t-header h1 { font-size: 16px; margin: 0; }
  .t-back { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .t-empty { padding: 40px; text-align: center; color: #888; }
  .t-placeholder {
    max-width: 720px; margin: 14px auto;
    padding: 12px 14px;
    background: rgba(255,159,64,0.08);
    border: 1px solid rgba(255,159,64,0.2);
    border-radius: 8px;
    color: #ffb066; font-size: 12px; text-align: left;
  }
  .t-placeholder code {
    font-family: ui-monospace, monospace; color: #c4b3ff; font-size: 11px;
  }
  .t-main { max-width: 720px; margin: 0 auto; padding: 16px; }
  .t-warn {
    background: rgba(255,170,68,0.1); border: 1px solid rgba(255,170,68,0.3);
    color: #fc6; padding: 10px 12px; border-radius: 6px; font-size: 12px; margin-bottom: 12px;
  }
  .t-warn code { background: rgba(0,0,0,0.3); padding: 1px 4px; border-radius: 3px; }
  .t-warn-inline { color: #fa8; font-size: 11px; margin: 4px 0; }
  .t-section { background: rgba(255,255,255,0.03); padding: 14px; border-radius: 6px; margin-bottom: 14px; }
  .t-section h2 { font-size: 13px; margin: 0 0 8px; color: #c4b3ff; }
  .t-section-h { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 6px; }
  .t-hint { font-size: 11px; color: #888; margin: 0 0 10px; line-height: 1.5; }
  .t-row {
    display: grid; grid-template-columns: 20px max-content 1fr;
    gap: 6px; align-items: center;
    padding: 6px 0; font-size: 12px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .t-row strong { color: #ccc; }
  .t-row small { color: #888; font-size: 11px; }
  .t-meta { font-size: 11px; color: #888; margin: 4px 0 0; }
  .t-btn, .t-btn-warn {
    background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 3px 10px; border-radius: 4px; cursor: pointer; font-size: 11px;
    margin-left: 4px;
  }
  .t-btn-warn { background: rgba(255,170,68,0.15); border-color: rgba(255,170,68,0.3); color: #fc6; }
  .t-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .t-table { width: 100%; border-collapse: collapse; font-size: 11px; }
  .t-table th { text-align: left; padding: 4px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .t-table td { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: top; }
  .t-table code { font-family: ui-monospace, monospace; }
  .t-payload { color: #888; font-size: 10px; max-width: 320px; display: inline-block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .t-ok { color: #0fc; }
  .t-pending { color: #fc6; font-size: 10px; }
</style>
