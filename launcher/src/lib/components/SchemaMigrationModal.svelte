<script lang="ts">
  // PR-37 (v0.2.12 / 2026-05-16): consent-gated schema-migration runner.
  //
  // Surfaces the two CLI-only schema migrations introduced in PR-24:
  //   1. migrate-development-temporal-props
  //      Adds the 4 canonical temporal date properties
  //      (created / updated / valid_from / valid_until) to every
  //      *_Development collection. Non-destructive — properties can be
  //      added retroactively via POST /v1/schema/<class>/properties.
  //   2. migrate-shared-kg-schema
  //      Drops + recreates the shared KG class when
  //      invertedIndexConfig.indexNullState is false. Weaviate <=1.30
  //      cannot add indexNullState retroactively; the only fix is a
  //      destructive recreate. Safe because the shared KG content
  //      derives from knowledge/**/*.md in the orchestrator clone —
  //      the drop + resync pass rebuilds from the .md sources.
  //
  // Consent flow:
  //   - On open: invoke `issue_schema_migration_consent_token` → UUID.
  //   - User clicks "Run migrations": invoke `run_schema_migrations`
  //     with the token. Backend validates the token was issued recently
  //     (in-memory map, 5min TTL, single-use). This prevents accidental
  //     re-runs from page reloads / stale Tauri events.
  //
  // Soft-fail: per-script outcomes surface in the report below; one
  // script failing doesn't roll back the other.

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { onMount } from 'svelte';

  interface DevelopmentCollectionSchema {
    class_name: string;
    temporal_props_present: string[];
    temporal_props_missing: string[];
  }
  interface SchemaMigrationStatusReport {
    weaviate_reachable: boolean;
    weaviate_url: string;
    development_collections: DevelopmentCollectionSchema[];
    shared_kg_class: string;
    shared_kg_exists: boolean | null;
    shared_kg_index_null_state: boolean | null;
    badge: string;
  }
  interface SchemaMigrationScriptOutcome {
    script: string;
    ok: boolean;
    exit_code: number | null;
    stdout: string;
    stderr: string;
  }
  interface SchemaMigrationReport {
    scripts: SchemaMigrationScriptOutcome[];
  }

  let {
    status,
    onClose,
    onCompleted,
  }: {
    status: SchemaMigrationStatusReport;
    onClose: () => void;
    onCompleted: () => void;
  } = $props();

  // Token state — fetched at mount time so the user's click only
  // round-trips the token back to the backend (no extra latency from a
  // token-fetch inside the run handler).
  let consentToken = $state<string | null>(null);
  let tokenError = $state<string | null>(null);
  let running = $state(false);
  let report = $state<SchemaMigrationReport | null>(null);

  onMount(async () => {
    try {
      consentToken = await invoke<string>('issue_schema_migration_consent_token');
    } catch (e) {
      tokenError = String(e);
    }
  });

  // Surface which actions each script will attempt — helps the user
  // make an informed-consent decision instead of staring at a generic
  // "Run migrations" button.
  const devMissingCount = $derived(
    status.development_collections.reduce(
      (acc, c) => acc + c.temporal_props_missing.length,
      0,
    ),
  );
  const sharedKgNeedsMigration = $derived(
    status.shared_kg_exists === true && status.shared_kg_index_null_state === false,
  );

  async function run() {
    if (!consentToken) {
      toast.error('No consent token issued — close and reopen this dialog');
      return;
    }
    running = true;
    try {
      const res = await invoke<SchemaMigrationReport>('run_schema_migrations', {
        consentToken,
      });
      report = res;
      // Single-use token: regardless of outcome, the backend has
      // consumed it. Refresh so a follow-up run requires reopening.
      consentToken = null;
      const failed = res.scripts.filter((s) => !s.ok);
      if (failed.length === 0) {
        toast.success('Schema migrations applied');
      } else {
        toast.error(
          `${failed.length} of ${res.scripts.length} migration script(s) failed; see report`,
        );
      }
      onCompleted();
    } catch (e) {
      toast.error(e);
    } finally {
      running = false;
    }
  }
</script>

<DialogRoot open={true} width="640px" onClose={onClose}>
  {#snippet header()}
    <div class="smm-header">
      <h3>Run schema migrations</h3>
      <p>
        Apply the v0.2.12 schema migrations to your Weaviate at
        <code>{status.weaviate_url}</code>. Both scripts are idempotent —
        re-running on an already-correct schema is a no-op.
      </p>
    </div>
  {/snippet}
  {#snippet body()}
    {#if !status.weaviate_reachable}
      <p class="smm-empty">
        Weaviate not reachable — start the services from the Services
        page before running migrations.
      </p>
    {:else if report}
      <section class="smm-section">
        <h4>Migration report</h4>
        {#each report.scripts as s (s.script)}
          <article class="smm-outcome" class:smm-outcome-ok={s.ok}>
            <header>
              <code>{s.script}</code>
              <span class="smm-badge smm-badge-{s.ok ? 'green' : 'red'}">
                {s.ok ? 'OK' : 'FAILED'}
              </span>
              {#if s.exit_code !== null}
                <span class="smm-exit">exit {s.exit_code}</span>
              {/if}
            </header>
            {#if s.stdout}
              <details>
                <summary>stdout ({s.stdout.length} chars)</summary>
                <pre>{s.stdout}</pre>
              </details>
            {/if}
            {#if s.stderr}
              <details open={!s.ok}>
                <summary>stderr ({s.stderr.length} chars)</summary>
                <pre>{s.stderr}</pre>
              </details>
            {/if}
          </article>
        {/each}
      </section>
    {:else}
      <section class="smm-section">
        <h4>What will run</h4>
        <ol class="smm-script-list">
          <li>
            <code>scripts/migrate-development-temporal-props</code>
            <p>
              Adds the 4 canonical temporal date properties
              (<code>created</code>, <code>updated</code>,
              <code>valid_from</code>, <code>valid_until</code>) to every
              <code>*_Development</code> collection that's missing them.
              Non-destructive — Weaviate accepts new properties via
              <code>POST /v1/schema/&lt;class&gt;/properties</code> on an
              existing class.
            </p>
            {#if devMissingCount > 0}
              <p class="smm-script-note">
                {devMissingCount} property addition{devMissingCount === 1 ? '' : 's'}
                across {status.development_collections.length} collection{status.development_collections.length === 1 ? '' : 's'}
                pending.
              </p>
            {:else}
              <p class="smm-script-note smm-note-ok">All Development collections already have the 4 temporal props.</p>
            {/if}
          </li>
          <li>
            <code>scripts/migrate-shared-kg-schema</code>
            <p>
              Drops + recreates the shared KG class
              (<code>{status.shared_kg_class}</code>) when
              <code>invertedIndexConfig.indexNullState</code> is false.
              Weaviate ≤1.30 cannot add <code>indexNullState</code>
              retroactively; the only fix is a destructive recreate. The
              shared KG content rebuilds from the orchestrator's
              <code>knowledge/**/*.md</code> sources during the script's
              <code>kg-sync --all</code> step.
            </p>
            {#if sharedKgNeedsMigration}
              <p class="smm-script-note smm-note-danger">
                ⚠ The shared KG class exists with
                <code>indexNullState=false</code>; this script will drop
                and recreate it.
              </p>
            {:else if status.shared_kg_exists === false}
              <p class="smm-script-note">Shared KG class does not exist yet — script is a no-op.</p>
            {:else}
              <p class="smm-script-note smm-note-ok">Shared KG already has <code>indexNullState=true</code>.</p>
            {/if}
          </li>
        </ol>

        <section class="smm-section smm-consent">
          <h4>Consent</h4>
          {#if tokenError}
            <p class="smm-error">Could not issue consent token: {tokenError}</p>
          {:else if !consentToken}
            <p class="smm-hint">Issuing consent token…</p>
          {:else}
            <p class="smm-hint">
              Token <code>{consentToken.slice(0, 8)}…{consentToken.slice(-4)}</code>
              issued for this dialog. Single-use, expires in 5 minutes.
            </p>
          {/if}
        </section>
      </section>
    {/if}
  {/snippet}
  {#snippet footer()}
    <div class="smm-footer">
      <button class="smm-btn" onclick={onClose} disabled={running}>
        {report ? 'Close' : 'Cancel'}
      </button>
      {#if !report}
        <button
          class="smm-btn smm-btn-primary"
          onclick={run}
          disabled={running || !consentToken || !status.weaviate_reachable}
        >
          {running ? 'Running…' : 'Run migrations'}
        </button>
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
  .smm-empty {
    color: #f5b342;
    padding: 24px;
    text-align: center;
    font-size: 12px;
  }
  .smm-section { margin-bottom: 14px; }
  .smm-section h4 {
    font-size: 12px;
    margin: 0 0 8px;
    color: #c4b3ff;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  .smm-script-list {
    list-style: decimal inside;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .smm-script-list > li {
    background: rgba(255,255,255,0.03);
    padding: 10px 12px;
    border-radius: 4px;
    border-left: 2px solid rgba(123,95,255,0.5);
  }
  .smm-script-list code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    background: rgba(255,255,255,0.07);
    padding: 1px 5px;
    border-radius: 3px;
    color: #c4b3ff;
  }
  .smm-script-list p {
    margin: 6px 0;
    font-size: 12px;
    color: #bbb;
    line-height: 1.5;
  }
  .smm-script-note {
    font-size: 11px;
    color: #888;
    margin: 4px 0 0;
    font-style: italic;
  }
  .smm-note-ok { color: #0fc; }
  .smm-note-danger {
    color: #f5b342;
    font-style: normal;
    font-weight: 500;
  }
  .smm-consent {
    margin-top: 16px;
    padding: 10px 12px;
    background: rgba(245,179,66,0.05);
    border-left: 2px solid rgba(245,179,66,0.35);
    border-radius: 4px;
  }
  .smm-hint { font-size: 11px; color: #aaa; margin: 0; }
  .smm-error { font-size: 11px; color: #f99; margin: 0; }
  .smm-outcome {
    background: rgba(255,255,255,0.03);
    border-left: 2px solid rgba(255,99,99,0.6);
    padding: 8px 12px;
    margin-bottom: 8px;
    border-radius: 4px;
  }
  .smm-outcome-ok {
    border-left-color: rgba(0,191,166,0.6);
  }
  .smm-outcome header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }
  .smm-outcome header code {
    flex: 1;
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #ddd;
    word-break: break-all;
  }
  .smm-badge {
    font-size: 10px;
    padding: 1px 8px;
    border-radius: 8px;
    font-weight: 600;
  }
  .smm-badge-green {
    background: rgba(0,191,166,0.18);
    color: #0fc;
  }
  .smm-badge-red {
    background: rgba(255,99,99,0.18);
    color: #f99;
  }
  .smm-exit { font-size: 10px; color: #888; }
  .smm-outcome details { margin-top: 4px; }
  .smm-outcome summary {
    font-size: 11px;
    color: #888;
    cursor: pointer;
  }
  .smm-outcome pre {
    margin: 4px 0 0;
    font-family: ui-monospace, monospace;
    font-size: 10px;
    color: #ccc;
    white-space: pre-wrap;
    max-height: 180px;
    overflow-y: auto;
    background: rgba(0,0,0,0.25);
    padding: 6px 8px;
    border-radius: 3px;
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
