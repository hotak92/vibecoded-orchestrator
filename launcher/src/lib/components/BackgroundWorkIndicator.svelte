<script lang="ts">
  // v0.2.106: the always-visible background-work indicator (MenuBar).
  //
  // Field report: after closing the "Update all" window nothing showed that
  // KG sync / code-graph / summary jobs were still running. This pill is
  // hidden while nothing runs; while something does, it shows a pulsing teal
  // ring and the count, and a click lists each job ("<project> — <what>")
  // with a link to that project's page, where its own banner has the detail.
  // Data: `stores/background-work.ts` (existing job events + status rows).
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { backgroundWork } from '$lib/stores/background-work';
  import { projects } from '$lib/stores/projects';
  import { bgJobLines, bgSummary } from '$lib/background-work';

  let open = $state(false);
  let wrapperEl: HTMLDivElement | undefined = $state();

  const nameById = $derived(new Map($projects.projects.map((p) => [p.id, p.name])));
  const lines = $derived(bgJobLines($backgroundWork, nameById));
  const summary = $derived(bgSummary($backgroundWork));

  // Seed from the status rows once the project list is known (and again for
  // any project added later — `seed` skips ids it has already read).
  $effect(() => {
    const ids = $projects.projects.map((p) => p.id);
    if (ids.length > 0) void backgroundWork.seed(ids);
  });

  // Close the list when the last job finishes.
  $effect(() => {
    if (summary === null) open = false;
  });

  onMount(() => {
    const onDocClick = (e: MouseEvent) => {
      if (open && wrapperEl && !wrapperEl.contains(e.target as Node)) open = false;
    };
    document.addEventListener('click', onDocClick);
    return () => document.removeEventListener('click', onDocClick);
  });

  function openProject(id: string) {
    open = false;
    projects.select(id);
    void goto(`/project/${encodeURIComponent(id)}`);
  }
</script>

{#if summary}
  <div class="bgw" bind:this={wrapperEl}>
    <button
      type="button"
      class="bgw-pill"
      aria-haspopup="true"
      aria-expanded={open}
      title="Background work is running — click for details"
      onclick={() => (open = !open)}
    >
      <span class="bgw-ring" aria-hidden="true"><span class="bgw-dot"></span></span>
      <span class="bgw-text">{summary}</span>
    </button>
    {#if open}
      <div class="bgw-pop" role="group" aria-label="Background work">
        <p class="bgw-head">Running in the background</p>
        <ul>
          {#each lines as l (l.key)}
            <li>
              <button type="button" class="bgw-row" onclick={() => openProject(l.project_id)}>
                <span class="bgw-name">{l.project_name}</span>
                <span class="bgw-label">{l.label}</span>
              </button>
            </li>
          {/each}
        </ul>
        <p class="bgw-hint">You can keep working; these finish on their own.</p>
      </div>
    {/if}
  </div>
{/if}

<style>
  .bgw { position: relative; display: inline-flex; }
  .bgw-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px 3px 6px; border-radius: 999px;
    background: rgba(0, 191, 166, 0.08);
    border: 1px solid rgba(0, 191, 166, 0.3);
    color: #f1f5f9; font: 600 11px/1.4 'Inter', system-ui, sans-serif;
    cursor: pointer;
    transition: background 0.2s ease, transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  }
  .bgw-pill:hover { background: rgba(0, 191, 166, 0.16); transform: translateY(-1px); }
  .bgw-ring { position: relative; width: 10px; height: 10px; }
  .bgw-ring::before {
    content: ''; position: absolute; inset: -3px; border-radius: 50%;
    background: rgba(0, 191, 166, 0.15);
    animation: bgw-pulse 2s ease-out infinite;
  }
  .bgw-dot {
    position: absolute; inset: 0; border-radius: 50%;
    background: linear-gradient(135deg, #00bfa6, #7b5fff);
  }
  @keyframes bgw-pulse {
    from { transform: scale(0.95); opacity: 0.6; }
    to { transform: scale(1.8); opacity: 0; }
  }
  @media (prefers-reduced-motion: reduce) {
    .bgw-ring::before { animation: none; }
  }
  .bgw-pop {
    position: absolute; top: calc(100% + 8px); right: 0; z-index: 400;
    min-width: 280px; max-width: 380px;
    padding: 10px 12px; border-radius: 14px;
    background: rgba(13, 23, 53, 0.97);
    border: 1px solid rgba(0, 191, 166, 0.25);
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.55);
  }
  .bgw-head {
    margin: 0 0 6px; color: #00bfa6;
    font: 600 11px/1.4 'Inter', system-ui, sans-serif;
    text-transform: uppercase; letter-spacing: 1.2px;
  }
  .bgw-pop ul { list-style: none; margin: 0; padding: 0; }
  .bgw-row {
    display: flex; flex-direction: column; gap: 1px; width: 100%;
    padding: 6px 8px; border-radius: 8px; border: none; text-align: left;
    background: transparent; color: #f1f5f9; cursor: pointer;
  }
  .bgw-row:hover { background: rgba(255, 255, 255, 0.04); }
  .bgw-name { font: 600 12px/1.4 'Inter', system-ui, sans-serif; }
  .bgw-label { color: #94a3b8; font: 400 11px/1.4 ui-monospace, 'SF Mono', Menlo, Consolas, monospace; }
  .bgw-hint { margin: 6px 0 0; color: #475569; font-size: 11px; font-style: italic; }
</style>
