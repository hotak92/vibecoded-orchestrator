<script lang="ts">
  /**
   * /preferences/chat-model-context — the version-keyed CHAT-model context
   * table (v0.2.92, WP-11).
   *
   * What the table does: the model gateway reads an exported copy of it and
   * advertises rows flagged 1M to Claude Code as `<id>[1m]`, so the client's
   * context indicator and /compact thresholds size correctly instead of
   * assuming a conservative default for an id it does not recognise.
   *
   * Keys are FULL model ids, never family patterns — `glm-5.2` has a 1M
   * window while `glm-5.1` has 200K, so a `glm-5*` rule would overstate the
   * smaller by 5x. That is the whole reason this is a table.
   *
   * NOT the embedding-model token limits (`MODEL_TOKEN_LIMITS` in
   * weaviate_mcp/chunking.py): those cover EMBEDDING models, set Ollama's
   * num_ctx for the chunker, and match partially on purpose.
   *
   * The pane keeps its logic in `$lib/api/chat_model_context` (validation +
   * the invoke wrappers) so the rules are unit-tested; this file is markup,
   * local UI state, and copy.
   */
  import { onMount } from 'svelte';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import {
    deleteChatModelContext,
    describeDelete,
    describeExport,
    describeReseed,
    draftFromRow,
    emptyDraft,
    exportChatModelContext,
    getChatModelContextStatus,
    listChatModelContext,
    reseedChatModelContext,
    upsertChatModelContext,
    validateDraft,
    type ChatModelContextDraft,
    type FieldError,
  } from '$lib/api/chat_model_context';
  import type {
    ChatModelContextMutation,
    ChatModelContextRow,
    ChatModelContextStatus,
  } from '$lib/types/chat-model-context';

  let rows = $state<ChatModelContextRow[]>([]);
  let status = $state<ChatModelContextStatus | null>(null);
  let loading = $state(true);
  let busy = $state(false);

  /** `model_id` of the row being edited, `'+'` for the new-row form. */
  let editing = $state<string | null>(null);
  let draft = $state<ChatModelContextDraft>(emptyDraft());
  let errors = $state<FieldError[]>([]);
  let pendingDelete = $state<string | null>(null);

  const oneMillionCount = $derived(rows.filter((r) => r.window_1m).length);

  function errorFor(field: FieldError['field']): string | null {
    return errors.find((e) => e.field === field)?.message ?? null;
  }

  async function load() {
    loading = true;
    try {
      [rows, status] = await Promise.all([
        listChatModelContext(),
        getChatModelContextStatus(),
      ]);
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  /** Every mutation carries its export outcome; a failed export is reported
   *  loudly because the row is saved while the gateway still serves the old
   *  file. */
  function reportExport(m: ChatModelContextMutation) {
    if (!m.export.ok) {
      toast.error(describeExport(m.export));
    }
  }

  function startAdd() {
    editing = '+';
    draft = emptyDraft();
    errors = [];
  }

  function startEdit(row: ChatModelContextRow) {
    editing = row.model_id;
    draft = draftFromRow(row);
    errors = [];
  }

  function cancelEdit() {
    editing = null;
    errors = [];
  }

  async function save() {
    const parsed = validateDraft(draft);
    if (!parsed.ok) {
      errors = parsed.errors;
      return;
    }
    errors = [];
    busy = true;
    try {
      const result = await upsertChatModelContext(parsed.input);
      reportExport(result);
      if (result.export.ok) {
        // Name the row the BACKEND stored, not the draft we sent: it is the
        // trimmed, validated id that is actually now the primary key.
        const saved = result.row?.model_id ?? parsed.input.model_id;
        toast.success(`Saved ${saved}. ${describeExport(result.export)}`);
      }
      editing = null;
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      busy = false;
    }
  }

  async function remove(row: ChatModelContextRow) {
    if (pendingDelete !== row.model_id) {
      pendingDelete = row.model_id;
      return;
    }
    busy = true;
    try {
      const result = await deleteChatModelContext(row.model_id);
      reportExport(result);
      if (result.export.ok) toast.success(describeDelete(row.model_id, result.deleted));
      pendingDelete = null;
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      busy = false;
    }
  }

  async function reseed() {
    busy = true;
    try {
      const result = await reseedChatModelContext();
      reportExport(result);
      if (result.reseed) toast.success(describeReseed(result.reseed));
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      busy = false;
    }
  }

  async function exportNow() {
    busy = true;
    try {
      const report = await exportChatModelContext();
      if (report.ok) toast.success(describeExport(report));
      else toast.error(describeExport(report));
      await load();
    } catch (e) {
      toast.error(e);
    } finally {
      busy = false;
    }
  }

  onMount(load);
</script>

<svelte:head>
  <title>Chat-model context — VCT Launcher</title>
</svelte:head>

<div class="page">
  <nav class="crumb">
    <a href="/preferences">Preferences</a>
    <span class="sep">/</span>
    <span class="current">Chat-model context</span>
  </nav>

  <header class="hdr">
    <h1>Chat-model context windows</h1>
    <p>
      Claude Code sizes its context indicator and its <code>/compact</code>
      thresholds from the window it believes the selected model has. For an id
      it does not recognise it assumes a conservative default, so a
      1M-context model reads as far fuller than it is and compaction fires
      early. The model gateway advertises the models flagged below as
      <code>&lt;id&gt;[1m]</code> — the client's own convention for the 1M
      variant — so the assumption is right.
    </p>
    <p class="hint">
      Rows are keyed by the <strong>full model id</strong>, never a family
      pattern: <code>glm-5.2</code> has a 1M window while
      <code>glm-5.1</code> has 200K, so a <code>glm-5*</code> rule would
      overstate the smaller one by five times. Every row cites the page its
      numbers came from — an uncited window is a guess, and the client would
      act on it.
    </p>
  </header>

  {#if loading && rows.length === 0}
    <p class="empty">Loading…</p>
  {:else}
    <section class="card status">
      <div class="status-grid">
        <div class="stat">
          <span class="stat-num">{status?.rows ?? rows.length}</span>
          <span class="stat-label">models</span>
        </div>
        <div class="stat">
          <span class="stat-num">{oneMillionCount}</span>
          <span class="stat-label">advertised 1M</span>
        </div>
        <div class="stat">
          <span class="stat-num">{status?.user_edited_rows ?? 0}</span>
          <span class="stat-label">edited by you</span>
        </div>
      </div>

      {#if status}
        <dl class="paths">
          <dt>Gateway reads</dt>
          <dd>
            <code>{status.export_path}</code>
            {#if status.export_path_overridden_by_env}
              <span class="badge badge-purple">VCT_MODEL_GATEWAY_CONTEXT_TABLE</span>
            {/if}
          </dd>
          <dt>Last exported</dt>
          <dd>
            {#if status.export_exists}
              <span class="mono">{status.export_generated_at ?? 'unknown'}</span>
              {#if status.export_models !== null}
                <span class="dim">· {status.export_models} model(s) in the file</span>
              {/if}
            {:else}
              <span class="dim">
                not written yet — the gateway is serving the copy shipped
                inside it, which is a normal state, not a fault.
              </span>
            {/if}
          </dd>
          {#if status.seed_path}
            <dt>Shipped defaults</dt>
            <dd>
              <code>{status.seed_path}</code>
              {#if !status.seed_available}
                <span class="dim">· not found</span>
              {/if}
            </dd>
          {/if}
        </dl>

        {#if status.export_problem}
          <p class="warn">
            The exported file is there but the gateway will not use it:
            {status.export_problem}
            This file is written by VCO from the table above, so overwriting it
            loses nothing you typed here.
          </p>
        {/if}
        {#if status.seed_problem}
          <p class="warn">Shipped defaults could not be read: {status.seed_problem}</p>
        {/if}
      {/if}

      <div class="actions">
        <button class="btn btn-primary" onclick={exportNow} disabled={busy}>
          Export now
        </button>
        <button class="btn" onclick={reseed} disabled={busy || status?.seed_available === false}>
          Reseed from shipped defaults
        </button>
        <button class="btn" onclick={startAdd} disabled={busy || editing === '+'}>
          Add a model
        </button>
      </div>
      <p class="hint">
        Reseeding re-applies the shipped rows and <strong>never touches a row
        you edited</strong> — those keep your values until you change them
        back. It restores a shipped row you deleted, and leaves models you
        added yourself alone.
      </p>
    </section>

    {#if editing === '+'}
      <section class="card editor">
        <h2>Add a model</h2>
        {@render editorFields()}
        <div class="actions">
          <button class="btn btn-primary" onclick={save} disabled={busy}>Add</button>
          <button class="btn" onclick={cancelEdit} disabled={busy}>Cancel</button>
        </div>
      </section>
    {/if}

    <section class="card">
      {#if rows.length === 0}
        <p class="empty">
          No models yet. The launcher seeds this table on first start from the
          shipped defaults; use “Reseed from shipped defaults” to populate it
          now, or add a model by hand.
        </p>
      {:else}
        <table class="tbl">
          <thead>
            <tr>
              <th>Model id</th>
              <th>Vendor</th>
              <th class="num">Context</th>
              <th class="num">Max output</th>
              <th>Advertise 1M</th>
              <th>Source</th>
              <th>Updated</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each rows as row (row.model_id)}
              {#if editing === row.model_id}
                <tr class="editing-row">
                  <td colspan="8">
                    {@render editorFields()}
                    <div class="actions">
                      <button class="btn btn-primary" onclick={save} disabled={busy}>
                        Save
                      </button>
                      <button class="btn" onclick={cancelEdit} disabled={busy}>Cancel</button>
                    </div>
                  </td>
                </tr>
              {:else}
                <tr>
                  <td>
                    <code>{row.model_id}</code>
                    {#if row.user_edited}
                      <span class="badge badge-teal" title="Reseed will leave this row alone">
                        edited
                      </span>
                    {/if}
                  </td>
                  <td class="dim">{row.vendor}</td>
                  <td class="num mono">{row.context_window.toLocaleString()}</td>
                  <td class="num mono">{row.max_output.toLocaleString()}</td>
                  <td>
                    {#if row.window_1m}
                      <span class="badge badge-teal">[1m]</span>
                    {:else}
                      <span class="dim">—</span>
                    {/if}
                  </td>
                  <td class="src">
                    <a href={row.source} target="_blank" rel="noreferrer noopener">
                      {row.source}
                    </a>
                    {#if row.source_note}
                      <span class="note" title={row.source_note}>note</span>
                    {/if}
                  </td>
                  <td class="dim mono">{row.updated_at}</td>
                  <td class="row-actions">
                    <button class="btn btn-sm" onclick={() => startEdit(row)} disabled={busy}>
                      Edit
                    </button>
                    {#if pendingDelete === row.model_id}
                      <button class="btn btn-sm btn-danger" onclick={() => remove(row)} disabled={busy}>
                        Confirm
                      </button>
                      <button class="btn btn-sm" onclick={() => (pendingDelete = null)} disabled={busy}>
                        Keep
                      </button>
                    {:else}
                      <button class="btn btn-sm btn-danger" onclick={() => remove(row)} disabled={busy}>
                        Remove
                      </button>
                    {/if}
                  </td>
                </tr>
                {#if row.source_note}
                  <tr class="note-row">
                    <td colspan="8"><span class="dim">{row.source_note}</span></td>
                  </tr>
                {/if}
              {/if}
            {/each}
          </tbody>
        </table>
      {/if}
    </section>
  {/if}

  <Toast />
</div>

{#snippet editorFields()}
  <div class="fields">
    <label class="field">
      <span>Model id</span>
      <input
        type="text"
        bind:value={draft.model_id}
        placeholder="glm-5.2"
        disabled={editing !== '+' && editing !== null}
        readonly={editing !== '+' && editing !== null}
      />
      <small class="field-hint">
        The full id the vendor serves. Renaming it here would create a second
        row, so it is fixed once the row exists.
      </small>
      {#if errorFor('model_id')}<small class="field-err">{errorFor('model_id')}</small>{/if}
    </label>

    <label class="field">
      <span>Vendor</span>
      <input type="text" bind:value={draft.vendor} placeholder="zai" />
      {#if errorFor('vendor')}<small class="field-err">{errorFor('vendor')}</small>{/if}
    </label>

    <label class="field">
      <span>Context window (tokens)</span>
      <input type="text" inputmode="numeric" bind:value={draft.context_window} placeholder="1000000" />
      {#if errorFor('context_window')}<small class="field-err">{errorFor('context_window')}</small>{/if}
    </label>

    <label class="field">
      <span>Max output (tokens)</span>
      <input type="text" inputmode="numeric" bind:value={draft.max_output} placeholder="128000" />
      {#if errorFor('max_output')}<small class="field-err">{errorFor('max_output')}</small>{/if}
    </label>

    <label class="field field-check">
      <input type="checkbox" bind:checked={draft.window_1m} />
      <span>Advertise as <code>[1m]</code></span>
      <small class="field-hint">
        Only for models whose official page states a 1M window. Flagging a
        200K model here makes Claude Code under-report how full it is.
      </small>
    </label>

    <label class="field field-wide">
      <span>Source</span>
      <input
        type="text"
        bind:value={draft.source}
        placeholder="https://docs.z.ai/guides/llm/glm-5.2"
      />
      <small class="field-hint">
        Where these numbers came from. Required — a row without a citation is
        ignored by the gateway rather than trusted.
      </small>
      {#if errorFor('source')}<small class="field-err">{errorFor('source')}</small>{/if}
    </label>

    <label class="field field-wide">
      <span>Source note (optional)</span>
      <input
        type="text"
        bind:value={draft.source_note}
        placeholder="e.g. the model has no page of its own; spec sits on the family card"
      />
    </label>
  </div>
{/snippet}

<style>
  .page {
    padding: 1.5rem;
    max-width: 1180px;
    margin: 0 auto;
  }

  .crumb {
    margin-bottom: 1rem;
    font-size: 0.9rem;
    color: var(--color-mid, #94a3b8);
  }
  .crumb a {
    color: var(--color-teal, #00bfa6);
    text-decoration: none;
  }
  .crumb a:hover {
    text-decoration: underline;
  }
  .crumb .sep {
    margin: 0 0.4rem;
    color: var(--color-border, rgba(255, 255, 255, 0.08));
  }
  .crumb .current {
    color: var(--color-text, #f1f5f9);
    font-weight: 500;
  }

  .hdr h1 {
    margin: 0 0 0.5rem;
    font-size: 1.5rem;
    font-weight: 700;
  }
  .hdr p {
    margin: 0 0 0.6rem;
    max-width: 74ch;
    line-height: 1.6;
    color: var(--color-mid, #94a3b8);
  }

  .hint {
    font-size: 0.88rem;
    color: var(--color-mid, #94a3b8);
    line-height: 1.55;
    max-width: 78ch;
  }

  .card {
    margin-top: 1.25rem;
    padding: 1.1rem 1.2rem;
    background: var(--color-card, rgba(255, 255, 255, 0.04));
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
    border-radius: 16px;
  }

  .status-grid {
    display: flex;
    gap: 2rem;
    margin-bottom: 0.9rem;
  }
  .stat {
    display: flex;
    flex-direction: column;
  }
  .stat-num {
    font-size: 1.6rem;
    font-weight: 700;
    color: var(--color-teal, #00bfa6);
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  }
  .stat-label {
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--color-mid, #94a3b8);
  }

  .paths {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.35rem 1rem;
    margin: 0 0 0.9rem;
    font-size: 0.87rem;
  }
  .paths dt {
    color: var(--color-mid, #94a3b8);
  }
  .paths dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .warn {
    margin: 0 0 0.9rem;
    padding: 0.6rem 0.75rem;
    border-radius: 10px;
    border: 1px solid rgba(255, 79, 160, 0.35);
    background: rgba(255, 79, 160, 0.08);
    color: var(--color-text, #f1f5f9);
    font-size: 0.87rem;
    line-height: 1.55;
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin: 0.6rem 0 0.4rem;
  }

  .btn {
    padding: 0.45rem 0.85rem;
    border-radius: 10px;
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
    background: var(--color-bg2, #080f28);
    color: var(--color-text, #f1f5f9);
    font-size: 0.87rem;
    font-weight: 600;
    cursor: pointer;
    transition: transform 120ms cubic-bezier(0.34, 1.56, 0.64, 1), border-color 160ms ease;
  }
  .btn:hover:not(:disabled) {
    border-color: rgba(var(--color-teal-rgb, 0, 191, 166), 0.5);
    transform: translateY(-1px);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-primary {
    background: var(--color-teal, #00bfa6);
    border-color: var(--color-teal, #00bfa6);
    color: #04121f;
  }
  .btn-primary:hover:not(:disabled) {
    background: var(--color-teal-hover, #00d4b8);
  }
  .btn-danger {
    border-color: rgba(255, 79, 160, 0.4);
    color: var(--color-pink, #ff4fa0);
  }
  .btn-sm {
    padding: 0.28rem 0.6rem;
    font-size: 0.8rem;
  }

  .tbl {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.87rem;
  }
  .tbl th {
    text-align: left;
    padding: 0.4rem 0.5rem;
    font-size: 0.76rem;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: var(--color-mid, #94a3b8);
    border-bottom: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
  }
  .tbl td {
    padding: 0.45rem 0.5rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    vertical-align: top;
  }
  .tbl .num {
    text-align: right;
  }
  .note-row td {
    padding-top: 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    font-size: 0.82rem;
  }
  .src a {
    color: var(--color-teal, #00bfa6);
    text-decoration: none;
    overflow-wrap: anywhere;
  }
  .src a:hover {
    text-decoration: underline;
  }
  .note {
    margin-left: 0.4rem;
    font-size: 0.72rem;
    color: var(--color-purple, #7b5fff);
    border-bottom: 1px dotted var(--color-purple, #7b5fff);
    cursor: help;
  }
  .row-actions {
    display: flex;
    gap: 0.35rem;
    justify-content: flex-end;
  }

  .badge {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0.05rem 0.4rem;
    border-radius: 999px;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.03em;
  }
  .badge-teal {
    background: rgba(var(--color-teal-rgb, 0, 191, 166), 0.15);
    color: var(--color-teal, #00bfa6);
  }
  .badge-purple {
    background: rgba(var(--color-purple-rgb, 123, 95, 255), 0.15);
    color: var(--color-purple, #7b5fff);
  }

  .fields {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: 0.85rem;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: 0.85rem;
  }
  .field > span {
    color: var(--color-mid, #94a3b8);
    font-weight: 600;
  }
  .field-wide {
    grid-column: 1 / -1;
  }
  .field-check {
    flex-direction: row;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.45rem;
  }
  .field input[type='text'] {
    padding: 0.4rem 0.55rem;
    border-radius: 8px;
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
    background: var(--color-bg2, #080f28);
    color: var(--color-text, #f1f5f9);
    font-size: 0.87rem;
  }
  .field input[type='text']:focus {
    outline: none;
    border-color: rgba(var(--color-teal-rgb, 0, 191, 166), 0.6);
  }
  .field input[readonly] {
    opacity: 0.6;
  }
  .field-hint {
    color: var(--color-muted, #475569);
    line-height: 1.45;
  }
  .field-err {
    color: var(--color-pink, #ff4fa0);
    line-height: 1.45;
  }

  .empty {
    color: var(--color-mid, #94a3b8);
    margin: 0;
  }
  .dim {
    color: var(--color-mid, #94a3b8);
  }
  .mono {
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  }
  code {
    font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
    font-size: 0.9em;
  }
</style>
