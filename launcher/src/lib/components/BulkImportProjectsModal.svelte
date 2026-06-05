<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // Scan-and-import modal.
  //
  // Pick a root folder, enumerate its immediate subdirectories (Rust:
  // scan_projects_under_root — the frontend can't read dirs, fs plugin is
  // off), present project-like candidates with checkboxes, then import the
  // selected ones by calling create_project_v2 ONCE PER FOLDER. Same
  // registration path as the single "Add Project" flow and as install.py —
  // no parallel code path.
  //
  // Import runs in the frontend loop (not a backend batch command) so each
  // project's row can flip pending → running → done/failed live. Sequential
  // on purpose: each create does bundle install + Weaviate bootstrap +
  // codegraph build + KG sync, which is heavy; running them concurrently
  // would hammer the embedding service.

  import { invoke } from '$lib/tauri';
  import { projects } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import { pickDirectory } from '$lib/dialog';
  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import type { ProjectHost } from '$lib/types/launcher';

  let { open = $bindable<boolean>(false) }: { open: boolean } = $props();

  interface ScannedCandidate {
    folder_path: string;
    name: string;
    already_registered: boolean;
    looks_like_project: boolean;
    is_orchestrator_clone: boolean;
    signals: string[];
  }
  interface ScanResult {
    root: string;
    candidates: ScannedCandidate[];
    unreadable: string[];
  }

  type ImportStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped';

  // Phases: pick → scanning → review (checkboxes) → importing → done.
  let phase = $state<'pick' | 'scanning' | 'review' | 'importing' | 'done'>('pick');
  let root = $state('');
  let scan = $state<ScanResult | null>(null);
  let scanError = $state<string | null>(null);
  // folder_path → selected
  let selected = $state<Record<string, boolean>>({});
  // folder_path → import status / error (during importing/done phases)
  let importState = $state<Record<string, { status: ImportStatus; error?: string }>>({});
  let importIndex = $state(0);
  let importTotal = $state(0);

  $effect(() => {
    if (open) {
      // Fresh state on every open.
      phase = 'pick';
      root = '';
      scan = null;
      scanError = null;
      selected = {};
      importState = {};
      importIndex = 0;
      importTotal = 0;
    }
  });

  const importable = $derived(
    (scan?.candidates ?? []).filter(
      (c) => !c.already_registered && !c.is_orchestrator_clone,
    ),
  );
  const selectedCount = $derived(
    Object.values(selected).filter(Boolean).length,
  );

  async function browseAndScan() {
    const picked = await pickDirectory({ title: 'Select a folder containing projects' });
    if (!picked) return;
    root = picked;
    phase = 'scanning';
    scanError = null;
    try {
      const result = await invoke<ScanResult>('scan_projects_under_root', { root: picked });
      scan = result;
      // Pre-select project-like, importable candidates by default.
      const pre: Record<string, boolean> = {};
      for (const c of result.candidates) {
        if (!c.already_registered && !c.is_orchestrator_clone && c.looks_like_project) {
          pre[c.folder_path] = true;
        }
      }
      selected = pre;
      phase = 'review';
    } catch (e) {
      scanError = e instanceof Error ? e.message : String(e);
      phase = 'review';
    }
  }

  function toggle(path: string) {
    selected = { ...selected, [path]: !selected[path] };
  }
  function selectAllImportable() {
    const next: Record<string, boolean> = { ...selected };
    for (const c of importable) next[c.folder_path] = true;
    selected = next;
  }
  function selectNone() {
    selected = {};
  }

  async function runImport() {
    const targets = importable.filter((c) => selected[c.folder_path]);
    if (targets.length === 0) return;
    phase = 'importing';
    importTotal = targets.length;
    importIndex = 0;
    // Seed all rows as pending.
    const seed: Record<string, { status: ImportStatus; error?: string }> = {};
    for (const t of targets) seed[t.folder_path] = { status: 'pending' };
    importState = seed;

    let ok = 0;
    let failed = 0;
    for (const t of targets) {
      importIndex += 1;
      importState = { ...importState, [t.folder_path]: { status: 'running' } };
      try {
        // Reuse the single-project create path verbatim. host 'base' = a
        // normal Claude-Code user project (the only host the Add modal
        // exposes). projects.create() also refreshes the store list.
        await projects.create(t.name, t.folder_path, 'base' as ProjectHost);
        importState = { ...importState, [t.folder_path]: { status: 'done' } };
        ok += 1;
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        importState = { ...importState, [t.folder_path]: { status: 'failed', error: msg } };
        failed += 1;
      }
    }
    phase = 'done';
    if (failed > 0) {
      toast.error(`Import finished: ${ok} added, ${failed} failed.`);
    } else {
      toast.success(`Import finished: ${ok} project${ok === 1 ? '' : 's'} added.`);
    }
  }

  function close() {
    open = false;
  }

  function rowIcon(s: ImportStatus): string {
    if (s === 'done') return '✓';
    if (s === 'failed') return '✗';
    if (s === 'running') return '…';
    return '–';
  }
</script>

<DialogRoot bind:open width="680px">
  {#snippet header()}
    <h3 class="bi-title">
      {#if phase === 'pick' || phase === 'scanning'}Scan &amp; import projects{/if}
      {#if phase === 'review'}Select projects to import{/if}
      {#if phase === 'importing'}Importing projects…{/if}
      {#if phase === 'done'}Import report{/if}
    </h3>
  {/snippet}

  {#snippet body()}
    {#if phase === 'pick'}
      <p class="bi-desc">
        Point at a folder that contains several project folders (e.g. your
        <code>code/</code> or <code>Projects/</code> directory). VCO scans its
        immediate subfolders, flags the ones that look like projects, and lets
        you import the selected ones in one pass.
      </p>
      <p class="bi-hint">
        Each import runs the full registration (bundle install, Weaviate
        bootstrap, codegraph build, KG sync), one project at a time — so a
        large batch can take a few minutes.
      </p>
    {/if}

    {#if phase === 'scanning'}
      <div class="bi-center">
        <div class="bi-spinner" aria-label="Scanning"></div>
        <p>Scanning <code>{root}</code>…</p>
      </div>
    {/if}

    {#if phase === 'review'}
      {#if scanError}
        <div class="bi-error"><strong>Scan failed:</strong><pre>{scanError}</pre></div>
      {:else if scan}
        <p class="bi-summary">
          Scanned <code>{scan.root}</code> —
          <strong>{importable.length}</strong> importable,
          <strong>{scan.candidates.length - importable.length}</strong> skipped
          (already registered, or the orchestrator clone).
        </p>
        <div class="bi-selbar">
          <button class="bi-link" onclick={selectAllImportable}>Select all</button>
          <button class="bi-link" onclick={selectNone}>Select none</button>
          <span class="bi-selcount">{selectedCount} selected</span>
        </div>
        <ul class="bi-rows">
          {#each scan.candidates as c (c.folder_path)}
            {@const disabled = c.already_registered || c.is_orchestrator_clone}
            <li class="bi-row" class:bi-row-disabled={disabled}>
              <input
                type="checkbox"
                checked={!!selected[c.folder_path]}
                disabled={disabled}
                onchange={() => toggle(c.folder_path)}
              />
              <div class="bi-row-main">
                <div class="bi-row-name">
                  {c.name}
                  {#if c.already_registered}<span class="bi-tag bi-tag-skip">already added</span>{/if}
                  {#if c.is_orchestrator_clone}<span class="bi-tag bi-tag-skip">orchestrator clone</span>{/if}
                  {#if !disabled && !c.looks_like_project}<span class="bi-tag bi-tag-warn">no project signals</span>{/if}
                </div>
                <div class="bi-row-path">{c.folder_path}</div>
                {#if c.signals.length > 0}
                  <div class="bi-row-signals">{c.signals.join(' · ')}</div>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
        {#if scan.candidates.length === 0}
          <p class="bi-hint">No subdirectories found under that folder.</p>
        {/if}
      {/if}
    {/if}

    {#if phase === 'importing' || phase === 'done'}
      <p class="bi-summary">
        {#if phase === 'importing'}
          Importing {importIndex} / {importTotal}…
        {:else}
          {Object.values(importState).filter((s) => s.status === 'done').length} added,
          {Object.values(importState).filter((s) => s.status === 'failed').length} failed.
        {/if}
      </p>
      <ul class="bi-rows">
        {#each Object.entries(importState) as [path, st] (path)}
          <li class="bi-row bi-irow bi-irow-{st.status}">
            <span class="bi-irow-icon">{rowIcon(st.status)}</span>
            <div class="bi-row-main">
              <div class="bi-row-path">{path}</div>
              {#if st.error}<div class="bi-irow-error" title={st.error}>{st.error}</div>{/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  {/snippet}

  {#snippet footer()}
    {#if phase === 'pick'}
      <button class="btn-ghost" onclick={close}>Cancel</button>
      <button class="btn-primary" onclick={browseAndScan}>Choose folder &amp; scan…</button>
    {:else if phase === 'scanning'}
      <button class="btn-ghost" disabled>Scanning…</button>
    {:else if phase === 'review'}
      <button class="btn-ghost" onclick={close}>Cancel</button>
      <button class="btn-primary" onclick={runImport} disabled={selectedCount === 0}>
        Import {selectedCount} project{selectedCount === 1 ? '' : 's'}
      </button>
    {:else if phase === 'importing'}
      <button class="btn-ghost" disabled>Importing…</button>
    {:else}
      <button class="btn-primary" onclick={close}>Close</button>
    {/if}
  {/snippet}
</DialogRoot>

<style>
  .bi-title { margin: 0; font-size: 16px; font-weight: 600; }
  .bi-desc { font-size: 13px; color: var(--color-mid, #aaa); line-height: 1.5; margin: 0 0 12px; }
  .bi-desc code, .bi-summary code, .bi-center code { font-family: ui-monospace, monospace; color: #c4b3ff; }
  .bi-hint { margin: 0; font-size: 11px; color: var(--color-muted, #888); line-height: 1.5; }
  .bi-center { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 24px 16px; text-align: center; }
  .bi-center p { margin: 0; font-size: 13px; }
  .bi-spinner {
    width: 28px; height: 28px;
    border: 3px solid rgba(0, 191, 166, 0.2); border-top-color: rgb(0, 191, 166);
    border-radius: 50%; animation: bi-spin 0.8s linear infinite;
  }
  @keyframes bi-spin { to { transform: rotate(360deg); } }
  .bi-error {
    background: rgba(255, 79, 160, 0.1); border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 6px; padding: 12px; color: var(--color-pink, #f99); font-size: 12px;
  }
  .bi-error pre { margin: 6px 0 0; font-size: 11px; white-space: pre-wrap; }
  .bi-summary { margin: 0 0 10px; font-size: 13px; }
  .bi-summary strong { margin: 0 2px; color: rgb(0, 191, 166); }
  .bi-selbar { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .bi-link {
    background: none; border: none; color: #c4b3ff; cursor: pointer;
    font-size: 12px; padding: 0; text-decoration: underline;
  }
  .bi-selcount { margin-left: auto; font-size: 11px; color: var(--color-muted, #888); }
  .bi-rows {
    list-style: none; padding: 0; margin: 0; max-height: 360px; overflow-y: auto;
    border: 1px solid rgba(255, 255, 255, 0.06); border-radius: 6px;
  }
  .bi-row {
    display: flex; align-items: flex-start; gap: 10px; padding: 8px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04); font-size: 12px;
  }
  .bi-row:last-child { border-bottom: none; }
  .bi-row input { margin-top: 2px; accent-color: rgb(0, 191, 166); }
  .bi-row-disabled { opacity: 0.55; }
  .bi-row-main { min-width: 0; flex: 1 1 auto; }
  .bi-row-name { font-weight: 600; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .bi-row-path {
    font-size: 11px; color: var(--color-muted, #888); font-family: ui-monospace, monospace;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 2px;
  }
  .bi-row-signals { font-size: 10px; color: #7b8; margin-top: 3px; }
  .bi-tag { font-size: 9px; text-transform: uppercase; letter-spacing: 0.4px; padding: 1px 6px; border-radius: 8px; }
  .bi-tag-skip { background: rgba(255, 255, 255, 0.08); color: #999; }
  .bi-tag-warn { background: rgba(255, 170, 80, 0.18); color: #fb8; }
  .bi-irow { display: grid; grid-template-columns: 22px 1fr; gap: 8px; align-items: start; }
  .bi-irow-icon {
    width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center;
    justify-content: center; font-size: 11px; font-weight: 700; margin-top: 1px;
  }
  .bi-irow-done .bi-irow-icon { color: rgb(0, 191, 166); background: rgba(0, 191, 166, 0.15); }
  .bi-irow-failed .bi-irow-icon { color: var(--color-pink, #f99); background: rgba(255, 79, 160, 0.15); }
  .bi-irow-running .bi-irow-icon { color: #c4b3ff; background: rgba(123, 95, 255, 0.18); }
  .bi-irow-pending .bi-irow-icon { color: #888; background: rgba(255, 255, 255, 0.06); }
  .bi-irow-error {
    font-size: 11px; color: var(--color-pink, #f99); background: rgba(255, 79, 160, 0.06);
    padding: 4px 6px; border-radius: 3px; margin-top: 4px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: help;
  }
  .btn-primary {
    padding: 6px 14px; background: rgb(0, 191, 166); color: #000; border: none;
    border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600;
  }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-ghost {
    padding: 6px 14px; background: rgba(255, 255, 255, 0.06); color: inherit;
    border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 4px; cursor: pointer;
    font-size: 12px; margin-right: 8px;
  }
  .btn-ghost:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
