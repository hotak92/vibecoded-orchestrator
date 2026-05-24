<script lang="ts">
  import { allEntries } from '$lib/glossary';

  const entries = allEntries();
</script>

<svelte:head>
  <title>Glossary — VCT Launcher</title>
</svelte:head>

<div class="page">
  <header class="page-header">
    <h1>Glossary</h1>
    <p class="lede">
      Plain-English explanations of every term you'll meet in the launcher.
    </p>
  </header>

  <div class="entries">
    {#each entries as entry}
      <article class="entry" id={entry.key}>
        <h2>
          <a href="#{entry.key}">{entry.label}</a>
        </h2>
        <p class="short">{entry.short}</p>
        {#if entry.long}
          <p class="long">{entry.long}</p>
        {/if}
        {#if entry.learnMore}
          <p class="learn">
            <a href={entry.learnMore.href} target="_blank" rel="noopener noreferrer"
              >Learn more: {entry.learnMore.label} ↗</a
            >
          </p>
        {/if}
      </article>
    {/each}
  </div>
</div>

<style>
  .page {
    padding: 28px 32px 60px;
    max-width: 760px;
    margin: 0 auto;
  }

  .page-header {
    margin-bottom: 28px;
  }

  h1 {
    font-size: 24px;
    font-weight: 800;
    color: var(--color-text);
    letter-spacing: -0.5px;
    margin-bottom: 6px;
  }

  .lede {
    font-size: 13px;
    color: var(--color-mid);
  }

  .entries {
    display: flex;
    flex-direction: column;
    gap: 18px;
  }

  .entry {
    padding: 16px 18px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
    scroll-margin-top: 80px;
  }

  .entry h2 {
    font-size: 15px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 8px;
  }
  .entry h2 a {
    color: inherit;
    text-decoration: none;
  }
  .entry h2 a:hover {
    color: var(--color-teal);
  }

  .short {
    font-size: 13px;
    color: var(--color-mid);
    line-height: 1.55;
    margin-bottom: 8px;
  }

  .long {
    font-size: 12.5px;
    color: var(--color-muted);
    line-height: 1.6;
    margin-bottom: 8px;
  }

  .learn {
    font-size: 12px;
    margin-top: 4px;
  }
  .learn a {
    color: var(--color-teal);
    text-decoration: none;
  }
  .learn a:hover {
    text-decoration: underline;
  }
</style>
