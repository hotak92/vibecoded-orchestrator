<script lang="ts">
  // PR-37 (v0.2.12 / 2026-05-16): MCP-page maintenance section.
  //
  // Mounts on /mcp under the Add/list area. Two status badges + two
  // action buttons:
  //
  //   1. Registration status — green/yellow/red badge based on
  //      `mcp_registration_status()`. Action: "Re-register MCPs" calls
  //      `rerun_mcp_registration()` (idempotent — already-correct
  //      entries are no-ops).
  //   2. Stale entries — green when zero, yellow when any exist.
  //      Action: opens StaleMcpModal for per-entry consent + rewrite.
  //
  // No badge auto-opens its modal — the GUI shows the colors and the
  // user clicks to drill in. Matches the consent-first pattern from
  // SchemaMigrationModal and the legacy-collections cleanup.

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import StaleMcpModal from '$lib/components/StaleMcpModal.svelte';

  interface McpRegistrationEntry {
    name: string;
    present: boolean;
    path_matches_install: boolean;
    command: string;
  }
  interface McpRegistrationStatusReport {
    install_root: string;
    claude_json_path: string;
    claude_json_exists: boolean;
    entries: McpRegistrationEntry[];
    badge: string;
  }
  interface RegistrationReport {
    claude_json_path: string;
    success_count: number;
    total: number;
    failures: string[];
    db_warnings: string[];
  }
  interface StaleMcpEntry {
    name: string;
    current_path: string;
    suggested_path: string;
  }

  let regStatus = $state<McpRegistrationStatusReport | null>(null);
  let regLoading = $state(true);
  let regRunning = $state(false);

  let stale = $state<StaleMcpEntry[]>([]);
  let staleLoading = $state(true);
  let showStaleModal = $state(false);

  async function refreshRegistration() {
    regLoading = true;
    try {
      regStatus = await invoke<McpRegistrationStatusReport>('mcp_registration_status');
    } catch (e) {
      toast.error(e);
      regStatus = null;
    } finally {
      regLoading = false;
    }
  }

  async function refreshStale() {
    staleLoading = true;
    try {
      stale = await invoke<StaleMcpEntry[]>('stale_mcp_entries');
    } catch (e) {
      toast.error(e);
      stale = [];
    } finally {
      staleLoading = false;
    }
  }

  async function rerun() {
    regRunning = true;
    try {
      const res = await invoke<RegistrationReport>('rerun_mcp_registration');
      if (res.failures.length === 0) {
        toast.success(
          `Re-registered ${res.success_count} of ${res.total} MCP(s) in ${res.claude_json_path}`,
        );
      } else {
        toast.error(
          `${res.failures.length} failure(s): ${res.failures.join('; ')}`,
        );
      }
      for (const w of res.db_warnings) toast.error(`DB warning: ${w}`);
    } catch (e) {
      toast.error(e);
    } finally {
      regRunning = false;
      await Promise.all([refreshRegistration(), refreshStale()]);
    }
  }

  async function onStaleCompleted() {
    showStaleModal = false;
    await Promise.all([refreshRegistration(), refreshStale()]);
  }

  onMount(async () => {
    await Promise.all([refreshRegistration(), refreshStale()]);
  });

  // Stale badge: green when zero, yellow when any exist. Stale entries
  // are always at least yellow — they indicate user-action-required
  // state, not a fatal error.
  const staleBadge = $derived(stale.length === 0 ? 'green' : 'yellow');
</script>

<section class="mm-section">
  <header class="mm-header">
    <h2>Maintenance</h2>
    <p class="mm-sub">
      MCP registration health + stale-entry cleanup. Wired to
      <code>~/.claude.json</code>; safe to re-run at any time.
    </p>
  </header>

  <!-- Registration status card -->
  <article class="mm-card">
    <header class="mm-card-h">
      <span class="mm-badge mm-badge-{regStatus?.badge ?? 'gray'}">
        {#if regLoading}
          checking…
        {:else if regStatus?.badge === 'green'}
          OK
        {:else if regStatus?.badge === 'yellow'}
          partial
        {:else if regStatus?.badge === 'red'}
          missing
        {:else}
          unknown
        {/if}
      </span>
      <strong>MCP registration</strong>
      <button
        class="mm-btn"
        onclick={rerun}
        disabled={regRunning || !regStatus}
      >
        {regRunning ? 'Re-registering…' : 'Re-register MCPs'}
      </button>
    </header>

    {#if regStatus}
      <p class="mm-meta">
        <code>{regStatus.claude_json_path}</code>
        {#if !regStatus.claude_json_exists}
          <span class="mm-tag mm-tag-warn">file does not exist yet</span>
        {/if}
      </p>
      {#if regStatus.install_root}
        <p class="mm-meta">
          install root: <code>{regStatus.install_root}</code>
        </p>
      {:else}
        <p class="mm-meta mm-meta-warn">
          install root not detected — re-register will fail until an
          install is registered.
        </p>
      {/if}
      <ul class="mm-entry-list">
        {#each regStatus.entries as e (e.name)}
          <li class="mm-entry">
            <code class="mm-entry-name">{e.name}</code>
            {#if !e.present}
              <span class="mm-tag mm-tag-err">missing</span>
            {:else if !e.path_matches_install}
              <span class="mm-tag mm-tag-warn">path mismatch</span>
            {:else}
              <span class="mm-tag mm-tag-ok">OK</span>
            {/if}
            {#if e.command}
              <code class="mm-entry-path">{e.command}</code>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </article>

  <!-- Stale entries card -->
  <article class="mm-card">
    <header class="mm-card-h">
      <span class="mm-badge mm-badge-{staleBadge}">
        {#if staleLoading}
          checking…
        {:else if stale.length === 0}
          none
        {:else}
          {stale.length} stale
        {/if}
      </span>
      <strong>Stale ~/.claude.json entries</strong>
      <button
        class="mm-btn"
        onclick={() => (showStaleModal = true)}
        disabled={staleLoading || stale.length === 0}
      >
        Review &amp; rewrite…
      </button>
    </header>
    <p class="mm-meta">
      Entries whose <code>command</code> or first arg looks like a
      vco install layout (<code>claude_mcp_servers/</code> or
      <code>.venv/</code> segments) but doesn't anchor on the current
      install root. Typically left behind when a previous install lived
      at a different path.
    </p>
    {#if !staleLoading && stale.length === 0}
      <p class="mm-meta mm-meta-ok">No stale entries detected.</p>
    {/if}
  </article>
</section>

{#if showStaleModal}
  <StaleMcpModal
    {stale}
    onClose={() => (showStaleModal = false)}
    onCompleted={onStaleCompleted}
  />
{/if}

<style>
  .mm-section {
    margin: 14px 24px 24px;
    padding: 14px 16px;
    background: rgba(255,255,255,0.02);
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.06);
  }
  .mm-header h2 {
    font-size: 13px;
    margin: 0;
    color: #c4b3ff;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .mm-sub {
    margin: 4px 0 14px;
    font-size: 11px;
    color: #888;
    line-height: 1.5;
  }
  .mm-sub code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }
  .mm-card {
    background: rgba(255,255,255,0.03);
    padding: 10px 12px;
    border-radius: 4px;
    margin-bottom: 10px;
  }
  .mm-card-h {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 6px;
  }
  .mm-card-h strong {
    font-size: 12px;
    flex: 1;
  }
  .mm-badge {
    font-size: 10px;
    padding: 2px 10px;
    border-radius: 10px;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.4px;
    cursor: default;
  }
  .mm-badge-green {
    background: rgba(0,191,166,0.18);
    color: #0fc;
  }
  .mm-badge-yellow {
    background: rgba(245,179,66,0.18);
    color: #f5b342;
  }
  .mm-badge-red {
    background: rgba(255,99,99,0.20);
    color: #f99;
  }
  .mm-badge-gray {
    background: rgba(255,255,255,0.08);
    color: #aaa;
  }
  .mm-btn {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 4px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 11px;
  }
  .mm-btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }
  .mm-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .mm-meta {
    margin: 4px 0;
    font-size: 11px;
    color: #888;
    line-height: 1.5;
  }
  .mm-meta-warn { color: #f5b342; }
  .mm-meta-ok { color: #0fc; }
  .mm-meta code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
    color: #c4b3ff;
  }
  .mm-tag {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 8px;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.4px;
  }
  .mm-tag-ok { background: rgba(0,191,166,0.18); color: #0fc; }
  .mm-tag-warn { background: rgba(245,179,66,0.18); color: #f5b342; }
  .mm-tag-err { background: rgba(255,99,99,0.18); color: #f99; }
  .mm-entry-list {
    list-style: none;
    margin: 6px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .mm-entry {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 6px;
    background: rgba(255,255,255,0.02);
    border-radius: 3px;
    font-size: 11px;
  }
  .mm-entry-name {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #ddd;
    min-width: 100px;
  }
  .mm-entry-path {
    font-family: ui-monospace, monospace;
    font-size: 10px;
    color: #888;
    word-break: break-all;
    flex: 1;
  }
</style>
