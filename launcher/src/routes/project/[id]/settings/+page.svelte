<script lang="ts">
  // v0.2.22 — Item #14. This standalone route used to host the entire
  // settings form (rename / update bundle / env-notes / danger zone).
  // The form has been extracted to `$lib/project-state/SettingsTab.svelte`
  // so it can be mounted directly inside the project page's "Settings"
  // tab (eliminating the prior two-click "Open project settings →"
  // hop). This page is kept as a thin wrapper so external links and
  // direct URLs (e.g. /project/abc123/settings) continue to work —
  // they just render the same component with its own back-arrow header.
  //
  // The wrapper exists rather than redirecting because:
  //   (a) browser back/forward through deep-linked settings URLs should
  //       not be interrupted by a redirect that then re-pushes history;
  //   (b) external tools (docs, support emails) may link directly here.

  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import SettingsTab from '$lib/project-state/SettingsTab.svelte';
  import type { ProjectView } from '$lib/types/launcher';

  let projectId = $derived($page.params.id);
  // Only loaded for the header title; the SettingsTab component loads
  // its own copy internally. Two reads of the same row is cheap (DB
  // lookup, no Weaviate calls).
  let project = $state<ProjectView | null>(null);
  // v0.2.103: a failed `get_project_v2` used to leave this route on a
  // silent, infinite "Loading…" (the catch only toasted) — and because
  // the route gates mounting SettingsTab on `project`, the whole tab was
  // unreachable. The error is now state, rendered inline with a Retry;
  // no timeout, so a slow-but-valid load is never cut off.
  let loadError = $state<string | null>(null);

  async function loadHeader() {
    loadError = null;
    try {
      project = await invoke<ProjectView>('get_project_v2', { id: projectId });
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
      toast.error(e);
    }
  }

  onMount(loadHeader);
  $effect(() => {
    if (projectId) void loadHeader();
  });
</script>

<div class="ps-page">
  <header class="ps-header">
    <button class="ps-back" onclick={() => goto(`/project/${projectId}`)} title="Return to project overview">
      ← Back to project
    </button>
    <h1>Settings — {project?.name ?? '…'}</h1>
  </header>

  <!-- Wait for the project lookup to complete before mounting SettingsTab.
       `$page.params.id` is typed `string | undefined` so we can't pass
       `projectId` directly without a non-null assertion; gating on
       `project` (which is loaded from the same id) gives us a real
       string AND avoids an extra "loading" state inside SettingsTab. -->
  {#if project}
    <SettingsTab projectId={project.id} />
  {:else if loadError}
    <div class="ps-load-error">
      <p class="ps-empty">Couldn't load this project's settings: {loadError}</p>
      <button class="ps-retry-btn" onclick={() => void loadHeader()}>Retry</button>
    </div>
  {:else}
    <p class="ps-empty">Loading…</p>
  {/if}
</div>

<Toast />

<style>
  .ps-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-text, #e8e8ee); }
  .ps-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .ps-header h1 { font-size: 16px; margin: 0; }
  .ps-back { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .ps-empty { padding: 40px; text-align: center; color: #888; }
  /* v0.2.103: the load-failure block — mirrors SettingsTab's own. Brand
     failure accent (pink) on the error text, teal Retry, per
     VCO_BRAND_REFERENCE.md; no new tokens. */
  .ps-load-error {
    display: flex; flex-direction: column; align-items: center; gap: 10px;
    padding: 40px 16px;
  }
  .ps-load-error .ps-empty { padding: 0; color: var(--color-pink, #ff4fa0); }
  .ps-retry-btn {
    background: var(--color-teal, #00bfa6); border: none; color: #001a17;
    padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px;
    font-weight: 600;
  }
</style>
