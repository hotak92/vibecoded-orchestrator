<script lang="ts">
  // v0.2.100: interrupted-project-move banner. Mounted in the shell's banner
  // stack; queries `list_live_project_moves_v2` once on mount (a live row at
  // that moment is, by construction, one a previous launcher session left
  // behind). Two sentences, because `running` (not moved) and `flipped`
  // (moved, clean-up owed) need opposite wording: see the logic module.
  // Informational tone (amber), never the red failure tone: nothing is lost.

  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import { projects } from '$lib/stores/projects';
  import { toast } from '$lib/stores/toast';
  import StatusBannerShell from './StatusBannerShell.svelte';
  import {
    buildMoveBannerItems,
    type LiveProjectMove,
  } from './project-move-banner-logic';

  let moves = $state<LiveProjectMove[]>([]);
  let dismissed = $state<Set<string>>(new Set());

  onMount(async () => {
    if (!tauriAvailable()) return;
    try {
      moves = await invoke<LiveProjectMove[]>('list_live_project_moves_v2');
    } catch (e) {
      // Passive banner: a failed read shows nothing rather than an error.
      console.debug('[vct] list_live_project_moves_v2 failed', e);
    }
  });

  const items = $derived(
    buildMoveBannerItems(
      moves,
      (id) => $projects.projects.find((p) => p.id === id)?.name ?? 'the project',
      dismissed,
    ),
  );

  function dismiss(id: string) {
    dismissed = new Set([...dismissed, id]);
  }

  async function copy(command: string) {
    try {
      await navigator.clipboard.writeText(command);
      toast.info('Command copied');
    } catch {
      toast.error('Could not copy the command - select it from the banner instead.');
    }
  }
</script>

{#each items as item (item.moveId)}
  <StatusBannerShell
    tone="warning"
    glyph="!"
    title={item.title}
    strongTitle
    phase={item.detail}
    detail={item.command}
  >
    {#snippet actions()}
      {#if item.command}
        <button
          type="button"
          class="bg-btn-primary"
          data-testid="move-banner-copy"
          onclick={() => copy(item.command ?? '')}
        >Copy command</button>
      {/if}
      <button
        type="button"
        class="bg-btn-x"
        aria-label="Dismiss banner"
        data-testid="move-banner-dismiss"
        onclick={() => dismiss(item.moveId)}
      >×</button>
    {/snippet}
  </StatusBannerShell>
{/each}
