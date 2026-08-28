<script lang="ts">
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Dropdown from '$lib/components/Dropdown.svelte';
  import {
    canToggle,
    gitVisibilityNote,
    isChecked,
    parseTimeoutSeconds,
    registerBlockedReason,
    settingsErrorBanner,
    stateLabel,
    stateTooltip,
    timeoutSeconds,
    unregisterConfirmText,
    type EffectiveHook,
    type EffectiveHooksView,
  } from './hooks-view';

  let { projectId }: { projectId: string } = $props();

  // v0.2.91 (decision #27): this tab used to render `project_hooks` rows and
  // write them back — a full placebo, because Claude Code's hook engine reads
  // `<project>/.claude/settings.json` and never looks at the launcher DB. The
  // view now comes from what that file ACTUALLY declares, and every mutation
  // edits it through one writer (`python -m vco_lib.hooks_settings`).
  let view = $state<EffectiveHooksView | null>(null);
  let loading = $state(true);
  let busyKey = $state<string | null>(null);
  let showAdd = $state(false);

  let nEvent = $state('PostToolUse');
  let nMatcher = $state('');
  let nCommand = $state('');
  let nTimeout = $state('');
  let nStarter = $state(true);

  const hooks = $derived(view?.hooks ?? []);
  const settingsPath = $derived(view?.settings_path ?? '.claude/settings.json');
  const readable = $derived(view?.settings_readable ?? false);
  const rowKey = (h: EffectiveHook) => `${h.event}\x00${h.matcher}\x00${h.command}`;
  const registerBlocked = $derived(registerBlockedReason(nEvent, nCommand, nTimeout));

  const COMMON_EVENTS = [
    'SessionStart', 'SessionEnd', 'UserPromptSubmit',
    'PreToolUse', 'PostToolUse', 'Stop', 'StopFailure',
    'PreCompact', 'PostCompact', 'TeammateIdle', 'TaskCompleted',
  ];
  const EVENT_OPTIONS = COMMON_EVENTS.map((e) => ({ value: e, label: e }));

  // PR-6 (v0.2.11): per-project lean-ctx toggle. Three logical states map
  // to two on-disk states for `<project>/.claude/env::VCO_LEAN_CTX_DEFAULT`:
  //   * 'default' → key absent (the PR-1 hook treats absence as "on")
  //   * 'on'      → key present, value 'on'
  //   * 'off'     → key present, value 'off'
  type LeanCtxChoice = 'default' | 'on' | 'off';
  const LEAN_CTX_KEY = 'VCO_LEAN_CTX_DEFAULT';
  const LEAN_CTX_OPTIONS: Array<{ value: LeanCtxChoice; label: string }> = [
    { value: 'default', label: 'Default (on)' },
    { value: 'on', label: 'Per-project: on' },
    { value: 'off', label: 'Per-project: off' },
  ];
  let leanCtxChoice = $state<LeanCtxChoice>('default');
  let leanCtxLoading = $state(true);
  let leanCtxSaving = $state(false);

  async function load() {
    loading = true;
    try {
      view = await invoke<EffectiveHooksView>('list_project_hooks_effective', { projectId });
    } catch (e) { toast.error(e); }
    finally { loading = false; }
  }

  // v0.2.22 item #17: "Re-scan from disk" button — see AgentsTab.svelte
  // for the full rationale. Hooks live in `.claude/settings.json`'s `hooks`
  // block; the populate scanner mirrors them into project_hooks. Since
  // v0.2.91 the rows shown here come from that FILE, not the mirror, so a
  // re-scan no longer changes what this tab displays — it refreshes the
  // metadata (source / paid-module attribution) the file cannot carry.
  let rescanning = $state(false);

  type RescanReport = {
    agents_inserted: number;
    skills_inserted: number;
    hooks_inserted: number;
    mcp_servers_inserted: number;
    kg_access_rows_inserted: number;
    warnings: string[];
  };

  async function rescan() {
    rescanning = true;
    try {
      const r = await invoke<RescanReport>('rescan_project_from_filesystem', {
        projectId,
      });
      const parts: string[] = [];
      if (r.agents_inserted > 0) parts.push(`${r.agents_inserted} agents`);
      if (r.skills_inserted > 0) parts.push(`${r.skills_inserted} skills`);
      if (r.hooks_inserted > 0) parts.push(`${r.hooks_inserted} hooks`);
      if (r.mcp_servers_inserted > 0) parts.push(`${r.mcp_servers_inserted} MCP servers`);
      toast.success(
        parts.length > 0
          ? `Re-scanned from disk: ${parts.join(', ')}`
          : 'Re-scan complete (nothing new to register)'
      );
      if (r.warnings.length > 0) {
        console.warn('[rescan] warnings:', r.warnings);
      }
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      rescanning = false;
    }
  }

  async function loadLeanCtx() {
    leanCtxLoading = true;
    try {
      const v = await invoke<string | null>('get_claude_env_value', {
        projectId,
        key: LEAN_CTX_KEY,
      });
      if (v === null || v === undefined) leanCtxChoice = 'default';
      else if (v === 'off') leanCtxChoice = 'off';
      else if (v === 'on') leanCtxChoice = 'on';
      // Any other value (manual edit) is rendered as "default" in the UI;
      // the user keeps the on-disk override until they actively change the
      // toggle, at which point we overwrite cleanly.
      else leanCtxChoice = 'default';
    } catch (e) {
      toast.error(e);
    } finally {
      leanCtxLoading = false;
    }
  }

  async function setLeanCtx(next: LeanCtxChoice) {
    if (next === leanCtxChoice) return;
    leanCtxSaving = true;
    const previous = leanCtxChoice;
    leanCtxChoice = next; // optimistic
    try {
      const value = next === 'default' ? null : next;
      await invoke('set_claude_env_value', {
        projectId,
        key: LEAN_CTX_KEY,
        value,
      });
      toast.success(
        next === 'default'
          ? 'Reverted to default (compression on)'
          : `Per-project compression set to ${next}`,
      );
    } catch (e) {
      leanCtxChoice = previous;
      toast.error(e);
    } finally {
      leanCtxSaving = false;
    }
  }

  async function toggle(hook: EffectiveHook, enabled: boolean) {
    busyKey = rowKey(hook);
    try {
      await invoke('set_project_hook_enabled', {
        projectId,
        event: hook.event,
        matcher: hook.matcher,
        command: hook.command,
        enabled,
      });
      toast.success(
        enabled
          ? `Restored to ${settingsPath} — it runs again.`
          : `Removed from ${settingsPath} — it has stopped running. Re-enable puts it back exactly.`,
      );
    } catch (e) {
      toast.error(e);
    } finally {
      busyKey = null;
      // Always re-read the file: on failure this proves nothing changed, on
      // success it proves what did.
      await load();
    }
  }

  async function del(hook: EffectiveHook) {
    if (!confirm(unregisterConfirmText(hook, settingsPath))) return;
    busyKey = rowKey(hook);
    try {
      await invoke('unregister_project_hook', {
        projectId,
        event: hook.event,
        matcher: hook.matcher,
        command: hook.command,
      });
      toast.success('Hook unregistered. The script file was not deleted.');
    } catch (e) {
      toast.error(e);
    } finally {
      busyKey = null;
      await load();
    }
  }

  async function add() {
    const blocked = registerBlockedReason(nEvent, nCommand, nTimeout);
    if (blocked) {
      toast.error(blocked);
      return;
    }
    const parsedTimeout = parseTimeoutSeconds(nTimeout);
    try {
      const outcome = await invoke<{
        changed: boolean;
        starter_path: string | null;
        starter_created: boolean;
      }>('register_project_hook', {
        projectId,
        req: {
          event: nEvent.trim(),
          matcher: nMatcher.trim(),
          command: nCommand.trim(),
          timeout_seconds: parsedTimeout.ok ? parsedTimeout.value : null,
          create_starter: nStarter,
        },
      });
      if (!outcome.changed) {
        toast.success(`That hook is already declared in ${settingsPath}.`);
      } else if (outcome.starter_created) {
        toast.success(`Hook wired into ${settingsPath}; starter script created at ${outcome.starter_path}.`);
      } else {
        toast.success(`Hook wired into ${settingsPath}.`);
      }
      nMatcher = ''; nCommand = ''; nTimeout = '';
      showAdd = false;
    } catch (e) {
      toast.error(e);
    } finally {
      await load();
    }
  }

  // Re-load on project switch. `$effect` fires on mount too, so there is no
  // separate `onMount` — a second load would only double the first render.
  $effect(() => { if (projectId) void load(); });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Hooks</h3>
    <button class="ps-btn-primary" disabled={!readable && !showAdd}
      onclick={() => (showAdd = !showAdd)}>{showAdd ? 'Cancel' : '+ Register'}</button>
  </header>

  <p class="ps-git-note">{gitVisibilityNote(settingsPath)}</p>

  {#if view && !readable}
    <div class="ps-banner" role="alert">
      <strong>Hook changes are unavailable.</strong>
      <span>{settingsErrorBanner(view.error_code, view.error, settingsPath)}</span>
      <button class="ps-btn-primary" onclick={load}>Reload</button>
    </div>
  {/if}

  {#if showAdd}
    <div class="ps-form">
      <div class="ps-form-grid">
        <label><span>Event</span>
          <Dropdown options={EVENT_OPTIONS} bind:value={nEvent} />
        </label>
        <label><span>Matcher</span><input bind:value={nMatcher} placeholder="Edit(*) — blank matches everything" /></label>
        <label class="ps-span2"><span>Command</span>
          <input bind:value={nCommand} placeholder="bash .claude/hooks/my-hook.sh" />
        </label>
        <label><span>Timeout (seconds)</span><input bind:value={nTimeout} placeholder="optional" inputmode="numeric" /></label>
      </div>
      <label class="ps-check">
        <input type="checkbox" bind:checked={nStarter} />
        <span>Create the hook script if it doesn't exist yet (never overwrites an existing file)</span>
      </label>
      <button class="ps-btn-primary" disabled={!readable || registerBlocked !== null} onclick={add}>
        Register hook
      </button>
      {#if registerBlocked}<span class="ps-blocked">{registerBlocked}</span>{/if}
    </div>
  {/if}

  {#if loading}
    <p class="ps-empty">Loading…</p>
  {:else if readable && hooks.length === 0}
    <div class="ps-empty-state">
      <p class="ps-empty">No hooks declared in <code>{settingsPath}</code>.</p>
      <p class="ps-empty-hint">
        Use <strong>+ Register</strong> to wire one up, or click Re-scan if you
        expected the launcher to already know about hooks in this project.
      </p>
      <button class="ps-btn-primary" disabled={rescanning} onclick={rescan}>
        {rescanning ? 'Re-scanning…' : 'Re-scan from disk'}
      </button>
    </div>
  {:else if readable}
    <table class="ps-table">
      <thead><tr><th>Event</th><th>Matcher</th><th>Command</th><th>Timeout</th><th>Source</th><th>State</th><th></th></tr></thead>
      <tbody>
        {#each hooks as h (rowKey(h))}
          <tr class:ps-row-orphan={h.state === 'orphan'}>
            <td><code>{h.event}</code></td>
            <td><code>{h.matcher || 'every'}</code></td>
            <td class="ps-cmd"><code>{h.command}</code></td>
            <td>{timeoutSeconds(h) === null ? '—' : `${timeoutSeconds(h)}s`}</td>
            <td><span class="ps-tag ps-tag-{h.source}">{h.source}</span></td>
            <td>
              <label class="ps-state" title={stateTooltip(h.state, settingsPath)}>
                <input
                  type="checkbox"
                  checked={isChecked(h)}
                  disabled={!canToggle(h, readable) || busyKey === rowKey(h)}
                  onchange={(e) => toggle(h, (e.target as HTMLInputElement).checked)} />
                <span class="ps-state-{h.state}">{stateLabel(h.state)}</span>
              </label>
            </td>
            <td>
              <button class="ps-btn-link" disabled={busyKey === rowKey(h)} onclick={() => del(h)}>
                {h.state === 'orphan' ? 'Clear record' : 'Unregister'}
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    {#if view && view.skipped.length > 0}
      <p class="ps-empty-hint">
        {view.skipped.length} entr{view.skipped.length === 1 ? 'y' : 'ies'} in
        <code>{settingsPath}</code> could not be listed (no <code>command</code> string):
        {view.skipped.join('; ')}
      </p>
    {/if}
  {/if}
</section>

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-form { background: rgba(255,255,255,0.04); padding: 12px; border-radius: 6px; margin-bottom: 16px; }
  .ps-form-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 10px; }
  .ps-span2 { grid-column: span 2; }
  .ps-form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-form-grid input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12);
    color: inherit; padding: 5px 8px; border-radius: 4px; font-size: 12px;
  }
  /* settings.json is usually VCS-tracked — say so before the user clicks. */
  .ps-git-note { color: #aaa; font-size: 11px; line-height: 1.5; margin: 0 0 12px; }
  .ps-banner {
    display: flex; flex-direction: column; gap: 6px; align-items: flex-start;
    background: rgba(255,120,120,0.10); border: 1px solid rgba(255,120,120,0.35);
    border-radius: 6px; padding: 12px; margin-bottom: 16px; font-size: 12px;
  }
  .ps-banner strong { color: #f99; }
  .ps-check { display: flex; align-items: center; gap: 8px; font-size: 11px; color: #aaa; margin-bottom: 10px; }
  .ps-blocked { color: #f99; font-size: 11px; margin-left: 10px; }
  .ps-state { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; cursor: help; }
  .ps-state input:disabled { cursor: not-allowed; }
  .ps-state-active { color: var(--color-teal); }
  .ps-state-disabled { color: #888; }
  .ps-state-orphan { color: #fc6; }
  .ps-row-orphan { opacity: 0.72; }
  .ps-btn-link:disabled { opacity: 0.5; cursor: not-allowed; text-decoration: none; }
  .ps-empty { color: #888; padding: 24px; text-align: center; }
  .ps-empty-state { text-align: center; padding: 24px; }
  .ps-empty-state .ps-empty { padding: 0 0 8px; }
  .ps-empty-hint { color: #aaa; font-size: 12px; padding: 0 0 16px; max-width: 480px; margin: 0 auto; }
  .ps-empty-hint code { background: rgba(255,255,255,0.08); padding: 1px 4px; border-radius: 3px; font-family: ui-monospace, monospace; }
  .ps-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 6px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .ps-table code { font-family: ui-monospace, monospace; font-size: 11px; }
  .ps-cmd { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ps-tag { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(255,255,255,0.08); color: #ccc; }
  .ps-tag-bundled { background: rgba(0,191,166,0.15); color: var(--color-teal); }
  .ps-tag-project { background: rgba(58,163,255,0.15); color: #6cf; }
  .ps-tag-paid-module { background: rgba(255,200,70,0.15); color: #fc6; }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link:hover { text-decoration: underline; }
</style>
