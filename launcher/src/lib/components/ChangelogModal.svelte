<script lang="ts">
  import { onMount } from 'svelte';
  import { marked } from 'marked';
  import DialogRoot from '$lib/components/DialogRoot.svelte';

  let {
    open = $bindable<boolean>(false),
  }: { open: boolean } = $props();

  const REPO = 'hotak92/vibecoded-orchestrator';
  const SEEN_KEY = 'vct.last_seen_changelog_version';

  let title = $state('');
  let body = $state('');
  let bodyHtml = $state('');
  let url = $state('');
  let loading = $state(false);
  let error = $state<string | null>(null);

  async function fetchLatest() {
    loading = true;
    error = null;
    try {
      const r = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`);
      if (!r.ok) {
        if (r.status === 404) {
          error = 'No releases yet on GitHub.';
          return;
        }
        throw new Error(`GitHub returned ${r.status}`);
      }
      const data = await r.json();
      title = data.name || data.tag_name || 'Latest release';
      body = data.body || '_(no release notes)_';
      url = data.html_url || `https://github.com/${REPO}/releases`;
      bodyHtml = await Promise.resolve(marked(body));
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  function close() {
    open = false;
    if (title) {
      try { localStorage.setItem(SEEN_KEY, title); } catch {}
    }
  }

  onMount(() => {
    if (open) void fetchLatest();
  });

  $effect(() => {
    if (open && !title && !loading) void fetchLatest();
  });
</script>

<!-- Bug 26: native <dialog> top-layer rendering via DialogRoot. -->
<DialogRoot bind:open width="640px" onClose={close}>
  {#snippet header()}
    <div class="cm-header">
      <h2>What's new</h2>
      <button class="cm-x" onclick={close} aria-label="Close">×</button>
    </div>
  {/snippet}
  {#snippet body()}
      {#if loading}
        <p class="cm-empty">Loading…</p>
      {:else if error}
        <p class="cm-error">{error}</p>
        <a class="cm-link" href={`https://github.com/${REPO}/releases`} target="_blank" rel="noreferrer">
          Open releases on GitHub →
        </a>
      {:else}
        <h3 class="cm-rel-title">{title}</h3>
        <div class="cm-body">{@html bodyHtml}</div>
        {#if url}
          <a class="cm-link" href={url} target="_blank" rel="noreferrer">View on GitHub →</a>
        {/if}
      {/if}
  {/snippet}
</DialogRoot>

<style>
  /* Bug 26: backdrop / sizing now handled by DialogRoot. */
  .cm-header { display: flex; justify-content: space-between; align-items: center; }
  .cm-header h2 { font-size: 14px; margin: 0; }
  .cm-x { background: none; border: none; color: #888; font-size: 20px; line-height: 1; cursor: pointer; padding: 0 4px; }
  .cm-x:hover { color: #fff; }
  .cm-empty, .cm-error { padding: 16px; text-align: center; color: #888; }
  .cm-error { color: #f99; }
  .cm-rel-title { color: #c4b3ff; font-size: 13px; margin: 4px 0 8px; }
  .cm-body {
    font-size: 12px; line-height: 1.55; color: #ccc;
    background: rgba(0,0,0,0.2); padding: 12px; border-radius: 6px;
  }
  .cm-body :global(h1), .cm-body :global(h2), .cm-body :global(h3) { color: #fff; font-size: 13px; margin: 12px 0 4px; }
  .cm-body :global(code) { font-family: ui-monospace, monospace; background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .cm-body :global(pre) { background: rgba(0,0,0,0.4); padding: 8px; border-radius: 4px; overflow-x: auto; }
  .cm-body :global(a) { color: #0fc; }
  .cm-body :global(ul) { padding-left: 20px; }
  .cm-link {
    display: inline-block; margin-top: 10px;
    color: #0fc; font-size: 11px; text-decoration: none;
  }
  .cm-link:hover { text-decoration: underline; }
</style>
