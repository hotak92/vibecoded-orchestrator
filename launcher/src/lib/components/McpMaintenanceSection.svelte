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
  import { invoke, safeInvoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import StaleMcpModal from '$lib/components/StaleMcpModal.svelte';
  // v0.2.91 WP-I: two cross-links onto this page, because this is where a user
  // looks when MCP rows seem wrong — the convergence engine's own deferral, and
  // the durable retirement badge the engine writes onto retired rows.
  import { selectedProject } from '$lib/stores/projects';
  import { deferrals } from '$lib/stores/deferrals';
  import { CID_CONVERGENCE_PENDING, findEntry } from '$lib/deferral-ledger';
  // The page's decisions live in a plain .ts so they are unit-testable without
  // jsdom (same split as codegraph-build-banner-logic.ts).
  import {
    isUserReEnabledAfterRetirement,
    npxState,
    retiredBadgeText,
    retiredRows,
    showsCannotSpawnTag,
  } from '$lib/components/mcp-maintenance-logic';

  interface McpRegistrationEntry {
    name: string;
    present: boolean;
    path_matches_install: boolean;
    command: string;
    // v0.2.91 WP-D: for an entry whose `command` is a BARE NAME resolved from
    // Claude Code's spawn PATH (e.g. `npx`). `true` it resolves, `false` it
    // does NOT (the MCP can never start), `null` not applicable (path-shaped
    // command) or the probe could not run. `null` NEVER turns the badge yellow
    // — positive evidence only.
    command_resolvable: boolean | null;
  }
  interface McpRegistrationStatusReport {
    install_root: string;
    claude_json_path: string;
    claude_json_exists: boolean;
    entries: McpRegistrationEntry[];
    badge: string;
    // v0.2.91 WP-D: `true`/`false` when the npx probe RAN, `null` when it could
    // not (no venv python, probe failed). "npx is missing" and "I could not
    // ask" must not render the same way — only the first is actionable.
    npx_present: boolean | null;
    /** Absolute npx path when resolvable, else empty. */
    npx_path: string;
    /** One-line remediation for the yellow-badge case (empty when green). */
    remediation: string;
  }
  // Mirrors `vct_launcher_core::db::project_mcp_servers::ProjectMcpServer`.
  // The subset the logic module needs is typed there (`ProjectMcpServerView`);
  // this is the full row as the Tauri command returns it.
  interface ProjectMcpServer {
    project_id: string;
    mcp_name: string;
    is_user_added: boolean;
    source: string;
    enabled: boolean;
    command: string | null;
    config: Record<string, unknown> | null;
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
  // PR-42 (v0.2.12): SIGHUP-driven env reload report shape.
  interface ReloadReport {
    signaled_count: number;
    pids: number[];
    errors: string[];
    posix_only_skipped: boolean;
  }

  let regStatus = $state<McpRegistrationStatusReport | null>(null);
  let regLoading = $state(true);
  let regRunning = $state(false);

  let stale = $state<StaleMcpEntry[]>([]);
  let staleLoading = $state(true);
  let showStaleModal = $state(false);

  // PR-42: in-flight flag for the "Reload MCPs" button.
  let reloading = $state(false);

  // v0.2.91 WP-I: retired MCP rows for the CURRENTLY SELECTED project. Scoped
  // and labelled — a retirement lives on one project's row, so showing it
  // without naming the project would be exactly the ambiguity decision #6's
  // rider forbids. No project selected ⇒ the card does not render.
  let retired = $state<ProjectMcpServer[]>([]);

  const rootLedger = $derived($deferrals);
  const convergenceEntry = $derived(
    findEntry(rootLedger.view, CID_CONVERGENCE_PENDING),
  );

  async function refreshRetired() {
    const projectId = $selectedProject?.id;
    if (!projectId) {
      retired = [];
      return;
    }
    // Soft read: a project whose rows cannot be listed is not an MCP problem
    // worth a toast on this page.
    const rows = await safeInvoke<ProjectMcpServer[]>('list_project_mcp_servers', {
      projectId,
    });
    retired = retiredRows(rows ?? []);
  }

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

  // PR-42 (v0.2.12 / 2026-05-16): manual SIGHUP-driven env reload.
  // Complements the auto-watcher (services/settings_json_watcher.rs):
  // the watcher fires automatically on .claude/settings.json edits, but
  // when the watcher is disabled OR the user just wants to force a
  // reload they can click this button. The MCPs exit cleanly with
  // sys.exit(0); Claude Code respawns them on the next request with
  // fresh env from settings.json.
  async function reloadMcps() {
    reloading = true;
    try {
      const res = await invoke<ReloadReport>('reload_mcps_sighup');
      if (res.posix_only_skipped) {
        toast.error(
          'SIGHUP reload is POSIX-only. To pick up env changes on Windows, ' +
            'restart your Claude Code chat session.',
        );
      } else if (res.signaled_count > 0) {
        toast.success(
          `Signaled ${res.signaled_count} MCP process(es) to reload. ` +
            `Claude Code will respawn them with fresh env on the next request.`,
        );
      } else if (res.errors.length === 0) {
        toast.success(
          'No running MCP processes to signal. New env will apply when ' +
            'Claude Code spawns the MCPs on the next request.',
        );
      }
      for (const err of res.errors) {
        if (!res.posix_only_skipped) {
          toast.error(`Reload warning: ${err}`);
        }
      }
    } catch (e) {
      toast.error(e);
    } finally {
      reloading = false;
    }
  }

  onMount(async () => {
    await Promise.all([
      refreshRegistration(),
      refreshStale(),
      refreshRetired(),
      // The convergence cross-link reads the ORCHESTRATOR-ROOT ledger through
      // the shared store, so this page and the MenuBar badge can never show a
      // different answer for the same file.
      deferrals.refreshRoot(),
    ]);
  });

  // Re-read retired rows whenever the selected project changes — the card is
  // per-project and must never keep showing the previous project's rows.
  $effect(() => {
    void $selectedProject?.id;
    void refreshRetired();
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
      <!-- PR-42: SIGHUP-driven env reload. Complementary to
           Re-register: Re-register updates ~/.claude.json paths,
           Reload signals running MCPs to pick up updated
           .claude/settings.json env. -->
      <button
        class="mm-btn"
        onclick={reloadMcps}
        disabled={reloading}
        title="Send SIGHUP to running MCPs so they exit cleanly and Claude Code respawns them with fresh env from .claude/settings.json"
      >
        {reloading ? 'Reloading…' : 'Reload MCPs (apply env changes)'}
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

      <!-- v0.2.91 WP-D: npx visibility. THREE states, three renderings —
           collapsing "npx is missing" into "I could not ask" is the bug this
           closes, because only the first is actionable. `null` says so
           plainly and never accuses the machine of anything. -->
      {#if npxState(regStatus) === 'present'}
        <p class="mm-meta mm-meta-ok">
          npx resolves: <code>{regStatus.npx_path}</code>
        </p>
      {:else if npxState(regStatus) === 'missing'}
        <p class="mm-meta mm-meta-warn">
          npx does not resolve — every npx-based MCP fails to spawn.
        </p>
      {:else}
        <p class="mm-meta">
          npx status unknown — the probe could not run (no orchestrator venv
          python resolved). This is not evidence that npx is missing.
        </p>
      {/if}
      {#if regStatus.remediation}
        <p class="mm-meta mm-meta-warn">{regStatus.remediation}</p>
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
            <!-- v0.2.91 WP-D: a registered, enabled MCP whose bare command
                 cannot be resolved is structurally incapable of starting.
                 Only `false` earns the tag — `null` (not applicable, or the
                 probe could not run) renders nothing, matching the badge's
                 positive-evidence-only rule. -->
            {#if showsCannotSpawnTag(e)}
              <span
                class="mm-tag mm-tag-err"
                title="This MCP's command is a bare name that does not resolve on PATH — Claude Code cannot spawn it."
              >
                cannot spawn
              </span>
            {/if}
            {#if e.command}
              <code class="mm-entry-path">{e.command}</code>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </article>

  <!-- v0.2.91 WP-E/WP-I: the convergence engine's ONE deferral, surfaced where
       MCP rows live. Renders ONLY when the orchestrator-root ledger actually
       holds the entry — a converged install shows nothing here, which is the
       point of the tiering (a standing "migration pending" FYI is the silt
       this release removes). Scope is named: this is an INSTALL-wide
       condition, not a fact about the selected project. -->
  {#if convergenceEntry}
    <article class="mm-card">
      <header class="mm-card-h">
        <span class="mm-badge mm-badge-yellow">pending</span>
        <strong>Project MCP rows not converged (orchestrator root)</strong>
      </header>
      <p class="mm-meta">{convergenceEntry.detected}</p>
      <p class="mm-meta">
        Full entry + Dismiss: Preferences → Updates → Pending actions.
      </p>
    </article>
  {/if}

  <!-- v0.2.91 WP-E/WP-I: retired MCP rows for the SELECTED project. A
       retirement is `enabled = 0` plus a durable badge — never a deletion — so
       a user who deliberately re-enables one keeps it. The card exists so a
       disabled row is explainable instead of mysterious. -->
  {#if $selectedProject && retired.length > 0}
    <article class="mm-card">
      <header class="mm-card-h">
        <span class="mm-badge mm-badge-gray">{retired.length} retired</span>
        <strong>Retired MCPs — project “{$selectedProject.name}”</strong>
      </header>
      <p class="mm-meta">
        These rows were retired by the convergence pass: disabled and badged,
        never deleted. Re-enabling one from the project's MCP toggles sticks —
        the pass will not retire it again.
      </p>
      <ul class="mm-entry-list">
        {#each retired as r (r.mcp_name)}
          <li class="mm-entry">
            <code class="mm-entry-name">{r.mcp_name}</code>
            <span class="mm-tag mm-tag-warn">{retiredBadgeText(r)}</span>
            {#if isUserReEnabledAfterRetirement(r)}
              <span
                class="mm-tag mm-tag-ok"
                title="You re-enabled this after it was retired — the convergence pass leaves it alone."
              >
                re-enabled by you
              </span>
            {/if}
          </li>
        {/each}
      </ul>
    </article>
  {/if}

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
