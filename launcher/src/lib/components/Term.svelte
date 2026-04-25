<script lang="ts">
  // Inline glossary tooltip. Wrap any jargon term:
  //   <Term key="code-function">CodeFunction</Term>
  //
  // Renders the children with a subtle dotted underline + a `?` icon.
  // Hovering or focusing reveals the short ELI5 from $lib/glossary.
  // The tooltip points at /glossary for full detail.

  import { getEntry } from '$lib/glossary';

  let {
    key,
    children,
  }: {
    key: string;
    children: import('svelte').Snippet;
  } = $props();

  const entry = $derived(getEntry(key));
</script>

<span class="term">
  {@render children()}
  {#if entry}
    <span class="bubble">
      <span class="title">{entry.label}</span>
      <span class="short">{entry.short}</span>
      <a class="more" href="/glossary#{key}">Glossary →</a>
    </span>
  {/if}
</span>

<style>
  .term {
    position: relative;
    border-bottom: 1px dotted rgba(255, 255, 255, 0.35);
    cursor: help;
  }

  .bubble {
    position: absolute;
    left: 0;
    bottom: calc(100% + 6px);
    z-index: 250;
    width: 280px;
    padding: 10px 12px;
    background: rgba(13, 23, 53, 0.98);
    backdrop-filter: blur(20px);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 10px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
    font-size: 12px;
    line-height: 1.5;
    color: var(--color-mid);
    display: none;
    flex-direction: column;
    gap: 4px;
    cursor: default;
    border-bottom: 1px solid rgba(255, 255, 255, 0.12);
  }

  .term:hover .bubble,
  .term:focus-within .bubble {
    display: flex;
  }

  .title {
    font-size: 11px;
    font-weight: 700;
    color: var(--color-text);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .short {
    color: var(--color-mid);
  }
  .more {
    margin-top: 2px;
    font-size: 11px;
    color: var(--color-teal);
    text-decoration: none;
  }
  .more:hover {
    text-decoration: underline;
  }
</style>
