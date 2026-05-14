<!--
  Bug H (v0.2.8 / Phase 5 of 2026-05-13 secrets migration plan):
  one-shot import surface for migrating on-disk secret stores into the
  launcher keychain.

  Value-handling rule (INVIOLABLE): this component only ever holds the
  KEY and the SOURCE descriptor — never the raw value. The backend
  reads the value from disk itself when `registerSecretFromSource` is
  called.

  Placement: minimal in-file stub. Drop into Preferences → "Secrets
  import" section OR add to the secrets management page. Wiring this
  into a real route is the v0.2.8 follow-up the launcher author can
  pick up after seeing the Rust commands land — the Rust side is the
  load-bearing change; the UI is plumbing.

  Usage example:
    <script>
      import SecretsImportPanel from '$lib/components/SecretsImportPanel.svelte';
    </script>
    <SecretsImportPanel />
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import {
    listImportableSecretKeys,
    registerSecretFromSource,
    type ImportableSecretKey
  } from '$lib/stores/secrets';

  let rows: ImportableSecretKey[] = [];
  let selected = new Set<string>(); // keyed by `${source}::${key}`
  let loading = false;
  let importing = false;
  let error: string | null = null;
  let results: Array<{ key: string; ok: boolean; msg?: string }> = [];

  function rowId(r: ImportableSecretKey): string {
    return `${r.source}::${r.key}`;
  }

  async function refresh(): Promise<void> {
    loading = true;
    error = null;
    try {
      rows = await listImportableSecretKeys();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  function toggle(r: ImportableSecretKey): void {
    const id = rowId(r);
    if (selected.has(id)) {
      selected.delete(id);
    } else {
      selected.add(id);
    }
    // Trigger reactivity.
    selected = new Set(selected);
  }

  async function importSelected(): Promise<void> {
    importing = true;
    results = [];
    for (const r of rows) {
      if (!selected.has(rowId(r))) continue;
      try {
        await registerSecretFromSource(r.key, r.source);
        results = [...results, { key: r.key, ok: true }];
      } catch (e) {
        results = [...results, { key: r.key, ok: false, msg: String(e) }];
      }
    }
    importing = false;
    // Re-enumerate so the "already_in_keychain" badges refresh.
    await refresh();
    selected = new Set();
  }

  onMount(refresh);
</script>

<section class="secrets-import">
  <header>
    <h3>Import existing secrets</h3>
    <button on:click={refresh} disabled={loading}>Refresh</button>
  </header>

  {#if error}
    <p class="error">Could not enumerate sources: {error}</p>
  {/if}

  {#if loading}
    <p>Scanning canonical sources&hellip;</p>
  {:else if rows.length === 0}
    <p>No importable secrets found on disk.</p>
  {:else}
    <ul class="rows">
      {#each rows as r (rowId(r))}
        <li>
          <label>
            <input
              type="checkbox"
              checked={selected.has(rowId(r))}
              disabled={r.already_in_keychain || importing}
              on:change={() => toggle(r)}
            />
            <span class="key">{r.key}</span>
            <span class="source">{r.source}</span>
            {#if r.already_in_keychain}
              <span class="badge">already imported</span>
            {/if}
          </label>
        </li>
      {/each}
    </ul>

    <button
      on:click={importSelected}
      disabled={importing || selected.size === 0}
    >
      Import selected ({selected.size})
    </button>
  {/if}

  {#if results.length > 0}
    <h4>Import results</h4>
    <ul class="results">
      {#each results as r}
        <li class:ok={r.ok} class:err={!r.ok}>
          {r.key}: {r.ok ? 'imported' : r.msg ?? 'failed'}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .secrets-import { display: flex; flex-direction: column; gap: 0.75rem; }
  header { display: flex; justify-content: space-between; align-items: center; }
  .rows { list-style: none; padding: 0; margin: 0; }
  .rows li { padding: 0.25rem 0; }
  .key { font-weight: 600; margin: 0 0.5rem; }
  .source { color: var(--muted, #888); font-size: 0.85em; }
  .badge { background: var(--accent-soft, #eef); padding: 0.1rem 0.4rem; border-radius: 0.2rem; font-size: 0.8em; margin-left: 0.5rem; }
  .error { color: var(--danger, #c00); }
  .results li.ok { color: var(--success, #060); }
  .results li.err { color: var(--danger, #c00); }
</style>
