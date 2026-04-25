<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { safeInvoke, invoke, isTauriRuntime } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';

  let info = $state<{ port: number; reachable: boolean } | null>(null);
  let apps = $state<any[]>([]);
  let catalog = $state<any[]>([]);
  let recipient = $state('');
  let messages = $state<any[]>([]);

  const inTauri = isTauriRuntime();

  async function load() {
    if (!inTauri) return; // Browser mode: render placeholder, no toast spam.
    try {
      info = await safeInvoke<{ port: number; reachable: boolean }>('hub_info');
    } catch (e) {
      toast.error(e);
    }
    try {
      const result = await safeInvoke<any>('hub_list_apps');
      apps = Array.isArray(result) ? result : (result?.apps ?? []);
    } catch (e) { console.warn(e); }
    try {
      const result = await safeInvoke<any>('hub_data_catalog');
      catalog = Array.isArray(result) ? result : (result?.entries ?? []);
    } catch (e) { console.warn(e); }
  }

  async function pollMessages() {
    if (!recipient.trim()) {
      toast.error('Pick a recipient first');
      return;
    }
    // Strict invoke here — user-initiated action, surface real errors.
    try {
      const result = await invoke<any>('hub_poll_messages', { recipient: recipient.trim() });
      messages = Array.isArray(result) ? result : (result.messages ?? []);
    } catch (e) { toast.error(e); }
  }

  onMount(load);
</script>

<div class="hub-page">
  <header class="hub-header">
    <button class="hub-back" onclick={() => goto('/')}>← Back</button>
    <h1>Orchestrator Hub</h1>
    {#if info}
      <span class="hub-meta">
        port <code>{info.port}</code>
        {#if info.reachable}<span class="hub-ok">reachable</span>
        {:else}<span class="hub-err">unreachable</span>{/if}
      </span>
    {/if}
    <button class="hub-refresh" onclick={load}>Refresh</button>
  </header>

  {#if !inTauri}
    <p class="hub-placeholder">
      The Orchestrator Hub requires the desktop app. Run
      <code>npm run tauri:dev</code> from <code>launcher/</code> to interact
      with apps, messages, and the data catalog.
    </p>
  {/if}

  <main class="hub-main">
    <section class="hub-section">
      <h2>Apps ({apps.length})</h2>
      {#if apps.length === 0}
        <p class="hub-empty">No apps registered.</p>
      {:else}
        <table class="hub-table">
          <thead><tr><th>App</th><th>Type</th><th>Status</th><th>Last seen</th></tr></thead>
          <tbody>
            {#each apps as a}
              <tr>
                <td><code>{a.app_id ?? a.name ?? '?'}</code></td>
                <td>{a.app_type ?? '—'}</td>
                <td>{a.status ?? '—'}</td>
                <td><small>{a.last_heartbeat_at ?? a.last_seen ?? '—'}</small></td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </section>

    <section class="hub-section">
      <header class="hub-section-h">
        <h2>Messages</h2>
        <div class="hub-poll">
          <select bind:value={recipient}>
            <option value="">— pick recipient —</option>
            {#each apps as a}
              <option value={a.app_id ?? a.name}>{a.app_id ?? a.name}</option>
            {/each}
          </select>
          <button class="hub-btn" onclick={pollMessages}>Poll</button>
        </div>
      </header>
      {#if messages.length === 0}
        <p class="hub-empty">No messages.</p>
      {:else}
        <table class="hub-table">
          <thead><tr><th>From</th><th>Type</th><th>At</th><th>Body</th></tr></thead>
          <tbody>
            {#each messages as m}
              <tr>
                <td><code>{m.sender ?? '?'}</code></td>
                <td>{m.message_type ?? '—'}</td>
                <td><small>{m.created_at ?? '—'}</small></td>
                <td><code class="hub-body">{JSON.stringify(m.body ?? m.payload ?? {}).slice(0, 120)}</code></td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </section>

    <section class="hub-section">
      <h2>Data catalog ({catalog.length})</h2>
      {#if catalog.length === 0}
        <p class="hub-empty">No data sources registered.</p>
      {:else}
        <ul class="hub-list">
          {#each catalog as c}
            <li><strong>{c.source_id ?? c.name ?? '?'}</strong> <small>{c.description ?? ''}</small></li>
          {/each}
        </ul>
      {/if}
    </section>
  </main>
</div>

<Toast />

<style>
  .hub-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .hub-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .hub-header h1 { font-size: 16px; margin: 0; }
  .hub-back, .hub-refresh, .hub-btn {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px;
  }
  .hub-meta { color: #888; font-size: 11px; }
  .hub-meta code { font-family: ui-monospace, monospace; color: #c4b3ff; }
  .hub-ok { color: #0fc; margin-left: 6px; }
  .hub-err { color: #f99; margin-left: 6px; }
  .hub-main { max-width: 900px; margin: 0 auto; padding: 16px; }
  .hub-section { background: rgba(255,255,255,0.03); padding: 14px; border-radius: 6px; margin-bottom: 14px; }
  .hub-section h2 { font-size: 13px; margin: 0 0 8px; color: #c4b3ff; }
  .hub-section-h { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; gap: 8px; }
  .hub-poll { display: flex; gap: 6px; align-items: center; }
  .hub-poll select {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 3px 8px; border-radius: 4px; font-size: 11px;
  }
  .hub-empty { color: #888; padding: 16px; text-align: center; font-size: 12px; }
  .hub-placeholder {
    margin: 14px 24px; padding: 12px 14px;
    background: rgba(255,159,64,0.08);
    border: 1px solid rgba(255,159,64,0.2);
    border-radius: 8px;
    color: #ffb066; font-size: 12px;
  }
  .hub-placeholder code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    color: #c4b3ff; font-size: 11px;
  }
  .hub-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .hub-table th { text-align: left; padding: 4px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .hub-table td { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .hub-table code { font-family: ui-monospace, monospace; }
  .hub-table small { color: #888; font-size: 10px; }
  .hub-body { color: #888; font-size: 10px; }
  .hub-list { list-style: none; padding: 0; margin: 0; font-size: 12px; }
  .hub-list li { padding: 4px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .hub-list small { color: #888; margin-left: 6px; }
</style>
