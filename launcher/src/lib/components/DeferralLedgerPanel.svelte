<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // v0.2.91 WP-I (decision #6) — the deferral-ledger panel.
  //
  // ONE component, mounted twice with DIFFERENT scopes:
  //
  //   * `<DeferralLedgerPanel scope="project" projectId={id} />`
  //     — on that project's Settings tab, showing ONLY that project's ledger;
  //   * `<DeferralLedgerPanel scope="orchestrator_root" />`
  //     — on Preferences → Updates, showing ONLY the orchestrator root's.
  //
  // The two never merge, never aggregate, and never render the same heading:
  // every string that names a scope comes from `deferral-ledger.ts`, which
  // derives it from the view's own `scope` / `scope_label` (the backend's, not
  // a guess about which page this happens to be on). That is the decision-#6 UX
  // rider in code rather than in convention.
  //
  // Groups: "Action needed" (the actionable partition — `action_required` +
  // `auto_retryable`) is open; "Records / by-design" collapses. Severity does
  // NOT decide the split — a completed repair record and genuinely pending work
  // were both `info` before WP-B, which is the confusion this panel exists to
  // end. The severity CHIP is still rendered per entry (it says how loud, the
  // disposition says what you owe), reusing `classify_warning`'s
  // error/warn/info vocabulary from `project_setup.rs`.
  //
  // The BADGE is narrower than the group: `action_required` only (user decision
  // 2026-08-27). `auto_retryable` entries stay in the open group with their
  // "VCO retries this itself" line and their retry trail — they are just not
  // counted, because a number the reader cannot act on is a nag. When the two
  // differ, `actionGroupNote` says so under the heading rather than leaving a
  // group of 3 under a badge of 1 to look like a counting bug.
  //
  // All decision logic is in `$lib/deferral-ledger` and unit-tested there (the
  // repo has no jsdom — wave-2 verdict 2). This file is markup over it.

  import { onMount } from 'svelte';
  import { toast } from '$lib/stores/toast';
  import {
    deferrals,
    dismissDeferral,
    loadProjectLedger,
  } from '$lib/stores/deferrals';
  import {
    actionGroupNote,
    badgeCount,
    dismissConfirmMessage,
    dismissResultMessage,
    dispositionExplanation,
    dispositionLabel,
    emptyStateMessage,
    groupEntries,
    mountConfigError,
    panelTitle,
    retryLine,
    sourceNotice,
    type DeferralLedgerView,
    type LedgerEntry,
    type LedgerScope,
  } from '$lib/deferral-ledger';

  let {
    scope,
    projectId = undefined,
  }: { scope: LedgerScope; projectId?: string } = $props();

  let view = $state<DeferralLedgerView | null>(null);
  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let dismissing = $state<string | null>(null);
  let recordsOpen = $state(false);

  const rootState = $derived($deferrals);

  // The root scope reads through the store so the MenuBar badge, this panel and
  // the MCP page cannot disagree about one file. The project scope owns its
  // own copy — there is no per-project cache by design.
  $effect(() => {
    if (scope === 'orchestrator_root') {
      view = rootState.view;
      loading = rootState.loading && !rootState.loaded;
      loadError = rootState.loaded && !rootState.view ? rootState.error : null;
    }
  });

  async function load() {
    // A `scope="project"` mount without a project id can never resolve a
    // folder: `load()` would call nothing, `view` would stay null, and the
    // empty state (`emptyStateMessage(null)`) would read "Loading…" forever.
    // No shipped mount does this — `SettingsTab` always passes the id — so this
    // is insurance that renders an explicit sentence instead of a lie.
    const configError = mountConfigError(scope, projectId);
    if (configError) {
      view = null;
      loadError = configError;
      loading = false;
      return;
    }
    loading = true;
    loadError = null;
    try {
      if (scope === 'orchestrator_root') {
        await deferrals.refreshRoot();
      } else if (projectId) {
        view = await loadProjectLedger(projectId);
      }
    } catch (e) {
      loadError = String(e);
      view = null;
    } finally {
      loading = false;
    }
  }

  async function dismiss(entry: LedgerEntry) {
    if (!view) return;
    // The confirmation names the entry AND the scope AND the folder — a
    // dismissal must never be mistakable for acting on the other ledger.
    if (!confirm(dismissConfirmMessage(entry, view))) return;
    dismissing = entry.condition_id;
    try {
      const outcome = await dismissDeferral(scope, entry.condition_id, projectId);
      const message = dismissResultMessage(outcome);
      if (outcome.dismissed) toast.success(message);
      else toast.info(message);
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      dismissing = null;
    }
  }

  onMount(load);

  const groups = $derived(groupEntries(view));
  const count = $derived(badgeCount(view));
  const groupNote = $derived(actionGroupNote(view));
  const notice = $derived(sourceNotice(view));
  const heading = $derived(
    view ? panelTitle(view) : scope === 'orchestrator_root'
      ? 'Pending actions — orchestrator root'
      : 'Pending actions',
  );

  function severityClass(severity: string): string {
    if (severity === 'critical') return 'dl-chip-err';
    if (severity === 'warning') return 'dl-chip-warn';
    return 'dl-chip-info';
  }
</script>

<section class="dl-section" data-scope={scope}>
  <header class="dl-header">
    <h2>{heading}</h2>
    <span class="dl-badge" class:dl-badge-zero={count === 0}>
      {#if loading}
        checking…
      {:else}
        {count} action{count === 1 ? '' : 's'} needed
      {/if}
    </span>
    <button class="dl-btn" onclick={load} disabled={loading}>
      {loading ? 'Loading…' : 'Refresh'}
    </button>
  </header>

  <p class="dl-scope">
    {#if scope === 'orchestrator_root'}
      Install-wide conditions from the orchestrator clone. Per-project entries
      live on each project's own Settings tab.
    {:else}
      Conditions deferred for this project only. Install-wide conditions live
      under Preferences → Updates.
    {/if}
    {#if view?.folder}
      <code class="dl-folder">{view.folder}</code>
    {/if}
  </p>

  {#if loadError}
    <p class="dl-notice dl-notice-err">{loadError}</p>
  {/if}
  {#if notice}
    <p class="dl-notice dl-notice-err">{notice}</p>
  {/if}
  {#each view?.warnings ?? [] as w (w)}
    <p class="dl-notice">{w}</p>
  {/each}

  {#if !loading && !loadError && groups.actionNeeded.length === 0 && groups.records.length === 0}
    <p class="dl-empty">{emptyStateMessage(view)}</p>
  {/if}

  {#if groups.actionNeeded.length > 0}
    <h3 class="dl-group-h">Action needed</h3>
    {#if groupNote}
      <!-- The badge counts `action_required` only, so the group can hold more
           entries than the number above it. Say why, rather than letting the
           gap read as a counting bug. -->
      <p class="dl-group-note">{groupNote}</p>
    {/if}
    <ul class="dl-list">
      {#each groups.actionNeeded as e (e.condition_id)}
        {@render entryCard(e)}
      {/each}
    </ul>
  {/if}

  {#if groups.records.length > 0}
    <details class="dl-records" bind:open={recordsOpen}>
      <summary>
        Records / by-design ({groups.records.length}) — nothing to do
      </summary>
      <ul class="dl-list">
        {#each groups.records as e (e.condition_id)}
          {@render entryCard(e)}
        {/each}
      </ul>
    </details>
  {/if}
</section>

{#snippet entryCard(e: LedgerEntry)}
  <li class="dl-entry">
    <div class="dl-entry-h">
      <span class="dl-chip {severityClass(e.severity)}">{e.severity}</span>
      <span class="dl-chip dl-chip-disp" class:dl-chip-auto={e.auto_retryable}>
        {dispositionLabel(e.disposition)}
      </span>
      <strong class="dl-title">{e.title}</strong>
      <code class="dl-cid">{e.condition_id}</code>
      <button
        class="dl-btn dl-btn-dismiss"
        onclick={() => dismiss(e)}
        disabled={dismissing === e.condition_id}
        title={view ? dismissConfirmMessage(e, view) : 'Dismiss this entry'}
      >
        {dismissing === e.condition_id ? 'Dismissing…' : 'Dismiss'}
      </button>
    </div>

    <p class="dl-disp-why">{dispositionExplanation(e)}</p>

    {#if e.detected}
      <p class="dl-body">{e.detected}</p>
    {/if}
    {#if e.why_deferred}
      <p class="dl-body dl-body-dim">{e.why_deferred}</p>
    {/if}

    {#if retryLine(e.retries)}
      <p class="dl-retry" class:dl-retry-capped={e.retries.cap_reached}>
        {retryLine(e.retries)}
      </p>
      {#if e.retries.outcomes.length > 0}
        <ul class="dl-attempts">
          {#each e.retries.outcomes as a, i (`${a.ts}-${a.status}-${i}`)}
            <li class="dl-attempt dl-attempt-{a.status}">
              <span class="dl-attempt-status">{a.status}</span>
              <span class="dl-attempt-ts">{a.ts}</span>
              <span class="dl-attempt-detail">{a.detail}</span>
            </li>
          {/each}
        </ul>
      {/if}
    {/if}

    {#if e.command_to_apply}
      <!-- Verbatim, including `#` comment lines and blank lines. The JSON
           sidecar is read precisely so these survive; re-wrapping or
           stripping them here would undo that. -->
      <pre class="dl-cmd">{e.command_to_apply}</pre>
    {/if}

    {#if e.kg_node_refs.length > 0}
      <p class="dl-refs">
        Context:
        {#each e.kg_node_refs as r (r)}<code>{r}</code>{/each}
      </p>
    {/if}
  </li>
{/snippet}

<style>
  .dl-section {
    margin: 14px 0 24px;
    padding: 14px 16px;
    background: rgba(255, 255, 255, 0.02);
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.06);
  }
  .dl-header {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .dl-header h2 {
    font-size: 13px;
    margin: 0;
    color: #c4b3ff;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    flex: 1;
  }
  .dl-badge {
    font-size: 10px;
    padding: 2px 10px;
    border-radius: 10px;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.4px;
    background: rgba(255, 79, 160, 0.18);
    color: #ff9ac7;
  }
  .dl-badge-zero {
    background: rgba(0, 191, 166, 0.18);
    color: #0fc;
  }
  .dl-scope {
    margin: 6px 0 12px;
    font-size: 11px;
    color: #888;
    line-height: 1.5;
  }
  .dl-folder {
    font-family: ui-monospace, monospace;
    font-size: 10px;
    color: #c4b3ff;
    background: rgba(255, 255, 255, 0.05);
    padding: 1px 4px;
    border-radius: 3px;
    margin-left: 6px;
    word-break: break-all;
  }
  .dl-notice {
    margin: 4px 0;
    font-size: 11px;
    color: #f5b342;
    line-height: 1.5;
  }
  .dl-notice-err {
    color: #f99;
  }
  .dl-empty {
    margin: 6px 0;
    font-size: 11px;
    color: #0fc;
  }
  .dl-group-h {
    font-size: 11px;
    margin: 10px 0 6px;
    color: #ddd;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .dl-group-note {
    margin: -2px 0 8px;
    font-size: 11px;
    color: #888;
    line-height: 1.5;
  }
  .dl-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .dl-entry {
    background: rgba(255, 255, 255, 0.03);
    border-radius: 4px;
    padding: 8px 10px;
  }
  .dl-entry-h {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .dl-title {
    font-size: 12px;
    flex: 1;
  }
  .dl-cid {
    font-family: ui-monospace, monospace;
    font-size: 10px;
    color: #888;
  }
  .dl-chip {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 8px;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.4px;
  }
  .dl-chip-err {
    background: rgba(255, 99, 99, 0.18);
    color: #f99;
  }
  .dl-chip-warn {
    background: rgba(245, 179, 66, 0.18);
    color: #f5b342;
  }
  .dl-chip-info {
    background: rgba(255, 255, 255, 0.08);
    color: #aaa;
  }
  .dl-chip-disp {
    background: rgba(123, 95, 255, 0.18);
    color: #b9a6ff;
  }
  .dl-chip-auto {
    background: rgba(0, 191, 166, 0.18);
    color: #0fc;
  }
  .dl-disp-why {
    margin: 5px 0 0;
    font-size: 11px;
    color: #b9a6ff;
    line-height: 1.5;
  }
  .dl-body {
    margin: 5px 0 0;
    font-size: 11px;
    color: #ccc;
    line-height: 1.5;
    white-space: pre-wrap;
  }
  .dl-body-dim {
    color: #888;
  }
  .dl-retry {
    margin: 6px 0 0;
    font-size: 11px;
    color: #f5b342;
    line-height: 1.5;
  }
  .dl-retry-capped {
    color: #f99;
  }
  .dl-attempts {
    list-style: none;
    margin: 4px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .dl-attempt {
    display: flex;
    gap: 8px;
    font-size: 10px;
    color: #888;
    font-family: ui-monospace, monospace;
  }
  .dl-attempt-status {
    min-width: 92px;
    text-transform: uppercase;
    font-weight: 600;
  }
  /* `inconclusive` gets its OWN colour — it is not a failure. */
  .dl-attempt-inconclusive .dl-attempt-status {
    color: #f5b342;
  }
  .dl-attempt-failed .dl-attempt-status {
    color: #f99;
  }
  .dl-attempt-retried .dl-attempt-status {
    color: #0fc;
  }
  .dl-attempt-ts {
    min-width: 150px;
  }
  .dl-attempt-detail {
    flex: 1;
    word-break: break-word;
  }
  .dl-cmd {
    margin: 6px 0 0;
    padding: 6px 8px;
    background: rgba(0, 0, 0, 0.35);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 10px;
    color: #ddd;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    overflow-x: auto;
  }
  .dl-refs {
    margin: 6px 0 0;
    font-size: 10px;
    color: #888;
  }
  .dl-refs code {
    font-family: ui-monospace, monospace;
    margin-right: 6px;
    color: #c4b3ff;
  }
  .dl-records {
    margin-top: 12px;
  }
  .dl-records summary {
    font-size: 11px;
    color: #888;
    cursor: pointer;
    margin-bottom: 6px;
  }
  .dl-btn {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: inherit;
    padding: 4px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 11px;
  }
  .dl-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
  }
  .dl-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .dl-btn-dismiss {
    font-size: 10px;
    padding: 2px 10px;
  }
</style>
