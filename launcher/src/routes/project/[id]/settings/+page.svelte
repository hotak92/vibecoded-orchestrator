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

  async function loadHeader() {
    try {
      project = await invoke<ProjectView>('get_project_v2', { id: projectId });
    } catch (e) {
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
</style>
