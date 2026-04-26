<script lang="ts">
  import { onMount } from 'svelte';
  import { marked } from 'marked';

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

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="cm-back" onclick={close}>
    <div class="cm-modal" onclick={(e) => e.stopPropagation()}>
      <header class="cm-header">
        <h2>What's new</h2>
        <button class="cm-x" onclick={close} aria-label="Close">×</button>
      </header>
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
    </div>
  </div>
{/if}

<style>
  /* Bug 19 systemic */
  .cm-back {
    position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 9999;
    display: flex; align-items: center; justify-content: center;
    padding: 2rem; overflow: hidden;
  }
  .cm-modal {
    background: #1a1a22; border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px; padding: 16px; width: 640px;
    max-width: min(92vw, 800px);
    max-height: calc(100vh - 4rem);
    display: flex; flex-direction: column; overflow: hidden;
  }
  .cm-modal > .cm-header { flex: 0 0 auto; }
  .cm-modal > .cm-body, .cm-modal > div:not(.cm-header) { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
  .cm-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
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
