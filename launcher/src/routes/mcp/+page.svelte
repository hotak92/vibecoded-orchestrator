<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke, safeInvoke, isTauriRuntime } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';

  // McpServerConfig — see launcher/src-tauri/src/types.rs
  interface McpServer {
    id: string;
    name: string;
    description: string;
    enabled: boolean;
    command: string;
    args: string[];
    env: Record<string, string>;
    min_tier: string;
    port: number | null;
    configurable: boolean;
  }

  let servers = $state<McpServer[]>([]);
  let loading = $state(true);

  let showAdd = $state(false);
  let nId = $state('');
  let nName = $state('');
  let nDescription = $state('');
  let nCommand = $state('');
  let nArgs = $state('');
  let nEnv = $state('');

  const inTauri = isTauriRuntime();

  async function load() {
    loading = true;
    try {
      // Soft read — null means browser preview, render placeholder.
      const result = await safeInvoke<McpServer[]>('get_mcp_servers');
      servers = result ?? [];
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  async function add() {
    if (!nId.trim() || !nCommand.trim()) {
      toast.error('id + command required');
      return;
    }
    const args = nArgs.split('\n').map((s) => s.trim()).filter(Boolean);
    const env: Record<string, string> = {};
    for (const line of nEnv.split('\n').map((s) => s.trim()).filter(Boolean)) {
      const eq = line.indexOf('=');
      if (eq < 0) continue;
      env[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
    }
    try {
      const updated = await invoke<McpServer[]>('add_custom_mcp_server', {
        server: {
          id: nId.trim(),
          name: nName.trim() || nId.trim(),
          description: nDescription.trim(),
          enabled: true,
          command: nCommand.trim(),
          args,
          env,
          min_tier: 'free',
          port: null,
          configurable: true,
          settings: {},
        },
      });
      servers = updated;
      toast.success('Server added');
      nId = nName = nDescription = nCommand = nArgs = nEnv = '';
      showAdd = false;
    } catch (e) {
      toast.error(e);
    }
  }

  async function remove(id: string) {
    if (!confirm(`Remove MCP server "${id}"?`)) return;
    try {
      const updated = await invoke<McpServer[]>('remove_mcp_server', { mcpId: id });
      servers = updated;
    } catch (e) { toast.error(e); }
  }

  async function toggle(id: string, enabled: boolean) {
    try {
      const updated = await invoke<McpServer[]>('toggle_mcp_server', {
        mcpId: id,
        enabled,
        userApps: [],
      });
      servers = updated;
    } catch (e) { toast.error(e); }
  }

  onMount(load);
</script>

<div class="mcp-page">
  <header class="mcp-header">
    <button class="mcp-back" onclick={() => goto('/')}>← Back</button>
    <h1>MCP Servers</h1>
    <button class="mcp-btn-primary" onclick={() => (showAdd = !showAdd)}>
      {showAdd ? 'Cancel' : '+ Add custom MCP server'}
    </button>
  </header>

  {#if showAdd}
    <div class="mcp-form">
      <div class="mcp-form-grid">
        <label><span>ID *</span><input bind:value={nId} placeholder="my-mcp" /></label>
        <label><span>Name</span><input bind:value={nName} placeholder="My MCP" /></label>
        <label class="mcp-span2"><span>Description</span>
          <input bind:value={nDescription} placeholder="What this MCP does" />
        </label>
        <label class="mcp-span2"><span>Command *</span>
          <input bind:value={nCommand} placeholder="/path/to/server.py or 'node'" />
        </label>
        <label><span>Args (one per line)</span>
          <textarea bind:value={nArgs} rows="3" placeholder="--port&#10;8080"></textarea>
        </label>
        <label><span>Env (KEY=VALUE per line)</span>
          <textarea bind:value={nEnv} rows="3" placeholder="API_KEY=secret"></textarea>
        </label>
      </div>
      <button class="mcp-btn-primary" onclick={add}>Save server</button>
    </div>
  {/if}

  {#if !inTauri}
    <p class="mcp-placeholder">
      Custom MCP servers require the desktop app. Run
      <code>npm run tauri:dev</code> from <code>launcher/</code> to add,
      remove, or toggle servers.
    </p>
  {/if}

  {#if loading}
    <p class="mcp-empty">Loading…</p>
  {:else if servers.length === 0}
    <p class="mcp-empty">No MCP servers configured.</p>
  {:else}
    <main class="mcp-main">
      {#each servers as s (s.id)}
        <article class="mcp-card">
          <header class="mcp-card-h">
            <strong>{s.name}</strong>
            <code class="mcp-id">{s.id}</code>
            <span class="mcp-tier mcp-tier-{s.min_tier}">{s.min_tier}</span>
            <input
              type="checkbox"
              checked={s.enabled}
              onchange={(e) => toggle(s.id, (e.target as HTMLInputElement).checked)}
            />
          </header>
          {#if s.description}<p class="mcp-desc">{s.description}</p>{/if}
          <p class="mcp-cmd"><code>{s.command} {s.args.join(' ')}</code></p>
          {#if s.configurable}
            <button class="mcp-btn-link" onclick={() => remove(s.id)}>Remove</button>
          {:else}
            <small>System-managed</small>
          {/if}
        </article>
      {/each}
    </main>
  {/if}
</div>

<Toast />

<style>
  .mcp-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .mcp-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .mcp-header h1 { font-size: 16px; margin: 0; flex: 1; }
  .mcp-back, .mcp-btn-primary, .mcp-btn-link {
    padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 500;
    border: 1px solid rgba(255,255,255,0.1); background: rgba(255,255,255,0.05); color: inherit;
  }
  .mcp-btn-primary { background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; font-weight: 600; }
  .mcp-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; }
  .mcp-btn-link:hover { text-decoration: underline; }
  .mcp-form { background: rgba(255,255,255,0.03); padding: 14px; margin: 14px 24px; border-radius: 6px; }
  .mcp-form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
  .mcp-span2 { grid-column: span 2; }
  .mcp-form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .mcp-form-grid input, .mcp-form-grid textarea {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 5px 8px; border-radius: 4px; font-size: 12px;
    font-family: inherit;
  }
  .mcp-form-grid textarea { font-family: ui-monospace, monospace; resize: vertical; }
  .mcp-empty { padding: 40px; text-align: center; color: #888; }
  .mcp-placeholder {
    margin: 14px 24px; padding: 12px 14px;
    background: rgba(255,159,64,0.08);
    border: 1px solid rgba(255,159,64,0.2);
    border-radius: 8px;
    color: #ffb066; font-size: 12px;
  }
  .mcp-placeholder code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    color: #c4b3ff; font-size: 11px;
  }
  .mcp-main { padding: 14px 24px; display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 10px; }
  .mcp-card { background: rgba(255,255,255,0.04); padding: 12px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.08); }
  .mcp-card-h { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  .mcp-card-h strong { font-size: 13px; flex: 1; }
  .mcp-id { font-family: ui-monospace, monospace; font-size: 10px; color: #888; }
  .mcp-tier { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(255,255,255,0.08); color: #ccc; }
  .mcp-tier-free { background: rgba(0,191,166,0.15); color: #0fc; }
  .mcp-tier-pro { background: rgba(123,95,255,0.15); color: #c4b3ff; }
  .mcp-desc { font-size: 11px; color: #888; margin: 4px 0; line-height: 1.4; }
  .mcp-cmd code { font-family: ui-monospace, monospace; font-size: 10px; color: #888; word-break: break-all; }
  .mcp-card small { color: #888; font-size: 10px; }
</style>
