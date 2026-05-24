<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke, safeInvoke, isTauriRuntime } from '$lib/tauri';
  import { selectedProject } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import NoProjectBanner from '$lib/components/NoProjectBanner.svelte';
  import type {
    CoordinationConfig,
    ConnectionTestResult,
    TeamStatus,
  } from '$lib/types/project-state';

  let config = $state<CoordinationConfig | null>(null);
  let loading = $state(true);
  let testing = $state(false);
  let test = $state<ConnectionTestResult | null>(null);
  let team = $state<TeamStatus | null>(null);
  let confirmingSchema = $state(false);

  // Form fields
  let supabaseUrl = $state('');
  let supabaseKey = $state(''); // write-only, never displayed
  let telegramToken = $state('');
  let username = $state('');
  let aliasesRaw = $state('');
  let telegramGroup = $state('');

  const project = $derived($selectedProject);
  const inTauri = isTauriRuntime();

  async function load() {
    if (!project) return;
    loading = true;
    try {
      // Soft read so browser preview doesn't toast-spam.
      const result = await safeInvoke<CoordinationConfig>('coordination_get_config', { projectId: project.id });
      config = result;
      if (result) {
        supabaseUrl = result.supabase_url ?? '';
        username = result.username ?? '';
        aliasesRaw = (result.user_aliases ?? []).join(', ');
        telegramGroup = result.telegram_group_id ?? '';
      }
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  async function save() {
    if (!project) return;
    try {
      await invoke('coordination_set_config', {
        projectId: project.id,
        update: {
          supabase_url: supabaseUrl || null,
          supabase_key: supabaseKey || null,
          telegram_bot_token: telegramToken || null,
          username: username || null,
          user_aliases: aliasesRaw.split(',').map((s) => s.trim()).filter(Boolean),
          telegram_group_id: telegramGroup || null,
        },
      });
      supabaseKey = '';
      telegramToken = '';
      toast.success('Saved');
      await load();
    } catch (e) {
      toast.error(e);
    }
  }

  async function runTest() {
    if (!project) return;
    testing = true;
    test = null;
    try {
      test = await invoke<ConnectionTestResult>('coordination_test_connection', { projectId: project.id });
    } catch (e) {
      toast.error(e);
    } finally {
      testing = false;
    }
  }

  async function applySchema() {
    if (!project) return;
    confirmingSchema = false;
    try {
      await invoke('coordination_apply_schema', { projectId: project.id });
      toast.success('Schema applied');
      await runTest();
    } catch (e) {
      toast.error(e);
    }
  }

  async function refreshTeam() {
    if (!project) return;
    try {
      team = await invoke<TeamStatus>('coordination_team_status', { projectId: project.id });
    } catch (e) {
      toast.error(e);
    }
  }

  onMount(load);
  $effect(() => { if (project) void load(); });
</script>

<svelte:head>
  <title>Coordination — VCT Launcher</title>
</svelte:head>

<div class="co-page">
  <header class="co-header">
    <button class="co-back" onclick={() => goto('/')}>← Back</button>
    <h1>Coordination</h1>
  </header>

  {#if !inTauri}
    <p class="co-empty co-placeholder">
      Coordination requires the desktop app. Run <code>npm run tauri:dev</code>
      from <code>launcher/</code> to configure Supabase and Telegram.
    </p>
  {:else if !project}
    <NoProjectBanner section="Coordination" />
  {:else if loading}
    <p class="co-empty">Loading…</p>
  {:else}
    <main class="co-main">
      <section class="co-section">
        <h2>Supabase</h2>
        <div class="co-form-grid">
          <label><span>Supabase URL</span>
            <input bind:value={supabaseUrl} placeholder="https://xyz.supabase.co" />
          </label>
          <label><span>Supabase service key</span>
            <input type="password" bind:value={supabaseKey}
              placeholder={config?.supabase_key_set ? '•••••• (already set)' : 'paste service key'} />
            <small>{config?.supabase_key_set ? 'Stored in keychain. Leave blank to keep.' : 'Required.'}</small>
          </label>
        </div>
      </section>

      <section class="co-section">
        <h2>Identity</h2>
        <div class="co-form-grid">
          <label><span>Team username</span>
            <input bind:value={username} placeholder="your-github-handle" />
          </label>
          <label><span>User aliases (comma)</span>
            <input bind:value={aliasesRaw} placeholder="alice, a" />
          </label>
        </div>
      </section>

      <section class="co-section">
        <h2>Telegram (optional)</h2>
        <div class="co-form-grid">
          <label><span>Bot token</span>
            <input type="password" bind:value={telegramToken}
              placeholder={config?.telegram_bot_token_set ? '•••••• (already set)' : 'optional'} />
          </label>
          <label><span>Group chat ID</span>
            <input bind:value={telegramGroup} placeholder="-100…" />
          </label>
        </div>
      </section>

      <div class="co-actions">
        <button class="co-btn-primary" onclick={save}>Save</button>
        <button class="co-btn" onclick={runTest} disabled={testing}>
          {testing ? 'Testing…' : 'Test connection'}
        </button>
        <button class="co-btn co-btn-warn" onclick={() => (confirmingSchema = true)}>
          Apply schema
        </button>
        <button class="co-btn" onclick={refreshTeam}>Refresh team status</button>
      </div>

      {#if test}
        <div class="co-test" class:co-ok={test.reachable && test.auth_ok && test.schema_applied}>
          <strong>{test.reachable ? 'Reachable' : 'Unreachable'}</strong>
          {#if test.latency_ms !== null}<span>· {test.latency_ms}ms</span>{/if}
          <span>· auth {test.auth_ok ? 'OK' : 'failed'}</span>
          <span>· schema {test.schema_applied ? 'applied' : 'missing'}</span>
          {#if test.error}<p class="co-test-err">{test.error}</p>{/if}
        </div>
      {/if}

      {#if team}
        <section class="co-section">
          <h2>Team status</h2>
          <p class="co-stat">
            {team.online_now} online · {team.members.length} member(s) · {team.recent_messages_count} recent messages
          </p>
          <div class="co-team-grid">
            {#each team.members as m}
              {@const online = team.presence.some((p) => p.username === m.username && p.status === 'online')}
              <div class="co-member" class:co-online={online}>
                <strong>{m.display_name || m.username}</strong>
                <small>@{m.username} · {m.role}</small>
              </div>
            {/each}
          </div>
        </section>
      {/if}

      {#if confirmingSchema}
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <div class="co-modal-back" onclick={() => (confirmingSchema = false)}>
          <div class="co-modal" onclick={(e) => e.stopPropagation()}>
            <h3>Apply coordination schema?</h3>
            <p>
              Runs <code>setup.py --non-interactive</code> against your Supabase. This is destructive
              if conflicting tables already exist with different shape — make a backup first.
            </p>
            <div class="co-modal-actions">
              <button class="co-btn" onclick={() => (confirmingSchema = false)}>Cancel</button>
              <button class="co-btn co-btn-warn" onclick={applySchema}>Apply</button>
            </div>
          </div>
        </div>
      {/if}
    </main>
  {/if}
</div>

<Toast />

<style>
  .co-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .co-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .co-header h1 { font-size: 16px; margin: 0; }
  .co-back { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .co-empty { padding: 40px; text-align: center; color: #888; }
  .co-placeholder {
    max-width: 720px; margin: 14px auto;
    padding: 12px 14px;
    background: rgba(255,159,64,0.08);
    border: 1px solid rgba(255,159,64,0.2);
    border-radius: 8px;
    color: #ffb066; font-size: 12px; text-align: left;
  }
  .co-placeholder code {
    font-family: ui-monospace, monospace; color: #c4b3ff; font-size: 11px;
  }
  .co-main { max-width: 720px; margin: 0 auto; padding: 16px; }
  .co-section { background: rgba(255,255,255,0.03); border-radius: 6px; padding: 14px; margin-bottom: 14px; }
  .co-section h2 { font-size: 13px; margin: 0 0 10px; color: #c4b3ff; }
  .co-form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .co-form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .co-form-grid input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 5px 8px; border-radius: 4px; font-size: 12px;
  }
  .co-form-grid small { font-size: 10px; color: #666; }
  .co-actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
  .co-btn, .co-btn-primary, .co-btn-warn {
    padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600;
    border: 1px solid rgba(255,255,255,0.15);
    background: rgba(255,255,255,0.05); color: inherit;
  }
  .co-btn-primary { background: rgb(0,191,166); border-color: rgb(0,191,166); color: #000; }
  .co-btn-warn { background: rgba(255,170,68,0.2); border-color: rgba(255,170,68,0.5); color: #fc6; }
  .co-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .co-test {
    padding: 10px 12px; border-radius: 6px;
    background: rgba(255,170,68,0.1); border: 1px solid rgba(255,170,68,0.3);
    color: #fc6; font-size: 12px; margin: 8px 0;
  }
  .co-test.co-ok { background: rgba(0,191,166,0.1); border-color: rgba(0,191,166,0.4); color: #0fc; }
  .co-test span { margin-left: 6px; }
  .co-test-err { font-size: 11px; color: #fa8; margin: 6px 0 0; }
  .co-stat { color: #888; font-size: 12px; margin: 0 0 8px; }
  .co-team-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; }
  .co-member {
    background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    padding: 8px 10px; border-radius: 6px; font-size: 12px;
  }
  .co-member.co-online { border-color: rgba(0,191,166,0.5); }
  .co-member small { display: block; color: #888; font-size: 10px; }
  .co-modal-back { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 9999; padding: 2rem; overflow: hidden; }
  .co-modal { background: #1a1a22; border-radius: 10px; padding: 16px; width: 420px; max-width: min(90vw, 600px); max-height: calc(100vh - 4rem); display: flex; flex-direction: column; overflow-y: auto; border: 1px solid rgba(255,255,255,0.08); }
  .co-modal h3 { font-size: 14px; margin: 0 0 8px; }
  .co-modal p { font-size: 12px; color: #ccc; line-height: 1.5; }
  .co-modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
</style>
