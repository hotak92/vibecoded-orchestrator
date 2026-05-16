<script lang="ts">
  // PR-37 (v0.2.12 / 2026-05-16): per-entry consent for rewriting stale
  // ~/.claude.json mcpServers entries.
  //
  // PR-33's CLI surface offered a y/n/all/skip-all prompt per detected
  // stale entry. This modal mirrors that contract as a row-per-entry
  // checkbox + "Rewrite Selected" / "Skip All" footer buttons. Default
  // state is unchecked = skip; the user has to opt in explicitly per
  // entry. "Select all" is a convenience toggle, NOT auto-action.
  //
  // The backend (`rewrite_stale_mcp_entries`) accepts a
  // Vec<(String, bool)> where each (mcp_name, should_rewrite) pair
  // encodes the per-entry consent. Unchecked entries pass through with
  // `should_rewrite=false` so the audit log captures the full set the
  // user reviewed, not just the ones they accepted.

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';

  interface StaleMcpEntry {
    name: string;
    current_path: string;
    suggested_path: string;
  }
  interface RewriteReport {
    claude_json_path: string;
    rewritten: string[];
    skipped: string[];
    errors: string[];
  }

  let {
    stale,
    onClose,
    onCompleted,
  }: {
    stale: StaleMcpEntry[];
    onClose: () => void;
    onCompleted: () => void;
  } = $props();

  // Per-entry checkbox state — keyed by mcp name. Initialized to false
  // (must opt in explicitly).
  let checked = $state<Record<string, boolean>>(
    Object.fromEntries(stale.map((e) => [e.name, false])),
  );
  let running = $state(false);
  let report = $state<RewriteReport | null>(null);

  const allChecked = $derived(stale.length > 0 && stale.every((e) => checked[e.name]));
  const someChecked = $derived(stale.some((e) => checked[e.name]));

  function toggleAll() {
    const target = !allChecked;
    checked = Object.fromEntries(stale.map((e) => [e.name, target]));
  }

  async function rewriteSelected() {
    if (!someChecked) {
      toast.error('No entries selected — check the rows you want to rewrite first');
      return;
    }
    running = true;
    try {
      // Always include EVERY entry in the consent payload (skipped =
      // false). Backend audit-logs the full reviewed set so a future
      // "why wasn't X rewritten?" question is answerable from logs.
      const consent: Array<[string, boolean]> = stale.map((e) => [
        e.name,
        checked[e.name] === true,
      ]);
      const res = await invoke<RewriteReport>('rewrite_stale_mcp_entries', { consent });
      report = res;
      if (res.errors.length > 0) {
        toast.error(`Rewrote ${res.rewritten.length}; ${res.errors.length} error(s) — see report`);
      } else {
        toast.success(
          `Rewrote ${res.rewritten.length} entry/entries (${res.skipped.length} skipped)`,
        );
      }
      onCompleted();
    } catch (e) {
      toast.error(e);
    } finally {
      running = false;
    }
  }

  function skipAll() {
    onClose();
  }
</script>

<DialogRoot open={true} width="720px" onClose={onClose}>
  {#snippet header()}
    <div class="smm-header">
      <h3>Rewrite stale MCP entries</h3>
      <p>
        Your <code>~/.claude.json</code> contains <strong>{stale.length}</strong>
        MCP entry/entries pointing at directories outside the current
        install root. These were typically left behind by a previous
        install at a different path. Check the entries you want to
        rewrite to point at the current install — anything left
        unchecked is preserved as-is.
      </p>
    </div>
  {/snippet}
  {#snippet body()}
    {#if report}
      <section class="smm-section">
        <h4>Rewrite report</h4>
        <p class="smm-hint">
          Wrote <code>{report.claude_json_path}</code> (backup at
          <code>{report.claude_json_path}.bak</code>).
        </p>
        {#if report.rewritten.length > 0}
          <p class="smm-line smm-line-ok">
            <strong>Rewritten:</strong>
            {report.rewritten.join(', ')}
          </p>
        {/if}
        {#if report.skipped.length > 0}
          <p class="smm-line">
            <strong>Skipped:</strong>
            {report.skipped.join(', ')}
          </p>
        {/if}
        {#if report.errors.length > 0}
          <p class="smm-line smm-line-err">
            <strong>Errors:</strong>
          </p>
          <ul class="smm-errors">
            {#each report.errors as err (err)}
              <li>{err}</li>
            {/each}
          </ul>
        {/if}
      </section>
    {:else}
      <section class="smm-section">
        <div class="smm-toolbar">
          <label class="smm-toolbar-label">
            <input
              type="checkbox"
              checked={allChecked}
              indeterminate={someChecked && !allChecked}
              onchange={toggleAll}
              disabled={running}
            />
            <span>{allChecked ? 'Unselect all' : 'Select all'}</span>
          </label>
          <span class="smm-toolbar-count">
            {Object.values(checked).filter(Boolean).length} of {stale.length} selected
          </span>
        </div>
        <ul class="smm-list">
          {#each stale as entry (entry.name)}
            <li class="smm-item">
              <label class="smm-item-row">
                <input
                  type="checkbox"
                  bind:checked={checked[entry.name]}
                  disabled={running || !entry.suggested_path}
                />
                <code class="smm-name">{entry.name}</code>
              </label>
              <div class="smm-paths">
                <div class="smm-path-line">
                  <span class="smm-path-label">current:</span>
                  <code class="smm-path-current">{entry.current_path}</code>
                </div>
                {#if entry.suggested_path}
                  <div class="smm-path-line">
                    <span class="smm-path-label">→ new:</span>
                    <code class="smm-path-suggested">{entry.suggested_path}</code>
                  </div>
                {:else}
                  <p class="smm-no-suggest">
                    No suggested rewrite — path shape unfamiliar to the
                    detector. Edit <code>~/.claude.json</code> by hand
                    or remove the entry from the MCP page.
                  </p>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="smm-footer">
      {#if !report}
        <button class="smm-btn" onclick={skipAll} disabled={running}>
          Skip All
        </button>
        <button
          class="smm-btn smm-btn-primary"
          onclick={rewriteSelected}
          disabled={running || !someChecked}
        >
          {running ? 'Rewriting…' : `Rewrite Selected (${Object.values(checked).filter(Boolean).length})`}
        </button>
      {:else}
        <button class="smm-btn" onclick={onClose}>Close</button>
      {/if}
    </div>
  {/snippet}
</DialogRoot>

<style>
  .smm-header h3 { margin: 0; font-size: 14px; }
  .smm-header p {
    margin: 6px 0 0;
    font-size: 12px;
    color: #aaa;
    line-height: 1.5;
  }
  .smm-header code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .smm-section { margin-bottom: 12px; }
  .smm-section h4 {
    font-size: 12px;
    margin: 0 0 8px;
    color: #c4b3ff;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .smm-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
    padding: 6px 10px;
    background: rgba(255,255,255,0.02);
    border-radius: 4px;
  }
  .smm-toolbar-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .smm-toolbar-count {
    font-size: 11px;
    color: #888;
  }
  .smm-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .smm-item {
    background: rgba(255,255,255,0.03);
    padding: 8px 10px;
    border-radius: 4px;
    border-left: 2px solid rgba(245,179,66,0.4);
  }
  .smm-item-row {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    margin-bottom: 4px;
  }
  .smm-name {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    color: #ffe296;
    font-weight: 500;
  }
  .smm-paths {
    margin-left: 22px;
  }
  .smm-path-line {
    display: flex;
    gap: 6px;
    font-size: 11px;
    margin-top: 2px;
    align-items: baseline;
  }
  .smm-path-label {
    color: #888;
    min-width: 56px;
    flex-shrink: 0;
  }
  .smm-path-current {
    font-family: ui-monospace, monospace;
    color: #f99;
    word-break: break-all;
  }
  .smm-path-suggested {
    font-family: ui-monospace, monospace;
    color: #0fc;
    word-break: break-all;
  }
  .smm-no-suggest {
    margin: 6px 0 0;
    font-size: 11px;
    color: #888;
    font-style: italic;
  }
  .smm-no-suggest code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }
  .smm-hint {
    font-size: 11px;
    color: #aaa;
    margin: 0 0 8px;
  }
  .smm-hint code {
    font-family: ui-monospace, monospace;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
  }
  .smm-line {
    font-size: 12px;
    margin: 4px 0;
    color: #ccc;
  }
  .smm-line-ok { color: #0fc; }
  .smm-line-err { color: #f99; }
  .smm-errors {
    list-style: disc inside;
    margin: 4px 0 0;
    padding: 0;
    font-size: 11px;
    color: #f99;
  }
  .smm-footer {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }
  .smm-btn {
    background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.12);
    color: inherit;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }
  .smm-btn:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }
  .smm-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .smm-btn-primary {
    background: rgb(0,191,166);
    border-color: rgb(0,191,166);
    color: #000;
    font-weight: 600;
  }
  .smm-btn-primary:hover:not(:disabled) {
    background: rgb(0,210,180);
  }
</style>
