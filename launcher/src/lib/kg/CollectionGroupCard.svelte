<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  import type { CollectionGroup } from './collection-grouping';

  let {
    group,
    onBrowse,
    onAccess,
  }: {
    group: CollectionGroup;
    /** Browse a single member collection. */
    onBrowse: (collection: string) => void;
    /** Open the access modal for ALL member collections of this group. */
    onAccess: (collections: string[]) => void;
  } = $props();

  let expanded = $state(false);

  const memberNames = $derived(group.members.map((m) => m.name));
  // The stack effect only reads when there is more than one member behind.
  const stackDepth = $derived(Math.min(group.members.length, 3));
</script>

<article class="gc" class:gc-expanded={expanded}>
  <!-- Stacked "cards behind" effect: one shim per extra member. -->
  {#if !expanded && stackDepth > 1}
    {#each Array(stackDepth - 1) as _, i}
      <div class="gc-shim" style="--i: {i + 1}"></div>
    {/each}
  {/if}

  <header class="gc-h">
    <button
      class="gc-toggle"
      onclick={() => (expanded = !expanded)}
      aria-expanded={expanded}
      title={expanded ? 'Collapse' : 'Expand members'}
    >
      <span class="gc-caret" class:gc-caret-open={expanded}>▸</span>
      <strong>{group.prefix}</strong>
    </button>
    {#if group.isShared}<span class="gc-badge gc-badge-shared">shared</span>{/if}
    <span class="gc-access gc-access-{group.access}">{group.access}</span>
  </header>

  <p class="gc-meta">
    {group.totalNodes} nodes · {group.members.length} collection{group.members.length === 1 ? '' : 's'}
  </p>

  {#if expanded}
    <ul class="gc-members">
      {#each group.members as m}
        <li class="gc-member">
          <span class="gc-member-role">{m.roleLabel}</span>
          <span class="gc-member-count">{m.node_count}</span>
          <button
            class="gc-member-browse"
            onclick={() => onBrowse(m.name)}
            disabled={m.access === 'none'}
            title={m.access === 'none' ? 'No read access' : `Browse ${m.name}`}
          >Browse</button>
        </li>
      {/each}
    </ul>
  {/if}

  <div class="gc-actions">
    <button class="gc-access-btn" onclick={() => onAccess(memberNames)}>
      Access… <span class="gc-all">(all {group.members.length})</span>
    </button>
  </div>
</article>

<style>
  .gc {
    position: relative;
    background: rgba(255, 255, 255, 0.04);
    padding: 10px 12px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }
  /* Each shim sits behind the card, offset down-right, to fake a stack. */
  .gc-shim {
    position: absolute;
    inset: 0;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    transform: translate(calc(var(--i) * 4px), calc(var(--i) * 4px));
    z-index: -1;
  }
  .gc-h { display: flex; align-items: center; gap: 6px; }
  .gc-toggle {
    display: flex; align-items: center; gap: 6px; flex: 1 1 auto; min-width: 0;
    background: none; border: none; color: inherit; cursor: pointer;
    padding: 0; font: inherit; text-align: left;
  }
  .gc-toggle strong {
    font-size: 13px; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .gc-caret { font-size: 10px; color: #888; transition: transform 0.12s ease; flex-shrink: 0; }
  .gc-caret-open { transform: rotate(90deg); }
  .gc-badge {
    font-size: 10px; padding: 1px 6px; border-radius: 8px;
    background: rgba(0, 191, 166, 0.15); color: #0fc; flex-shrink: 0;
  }
  .gc-access {
    font-size: 10px; padding: 1px 6px; border-radius: 8px;
    text-transform: uppercase; flex-shrink: 0;
  }
  .gc-access-read { background: rgba(123, 95, 255, 0.2); color: #c4b3ff; }
  .gc-access-write { background: rgba(0, 191, 166, 0.2); color: #0fc; }
  .gc-access-none { background: rgba(255, 99, 99, 0.15); color: #f99; }
  .gc-access-mixed { background: rgba(255, 170, 80, 0.18); color: #fb8; }
  .gc-meta { font-size: 11px; color: #888; margin: 4px 0 8px; }
  .gc-members { list-style: none; margin: 0 0 8px; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .gc-member {
    display: flex; align-items: center; gap: 8px;
    background: rgba(255, 255, 255, 0.03); border-radius: 4px; padding: 4px 8px;
  }
  .gc-member-role { font-size: 11px; flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gc-member-count { font-size: 10px; color: #888; flex-shrink: 0; }
  .gc-member-browse {
    background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.1);
    color: inherit; padding: 2px 8px; border-radius: 4px; cursor: pointer; font-size: 10px; flex-shrink: 0;
  }
  .gc-member-browse:hover { background: rgba(255, 255, 255, 0.12); }
  .gc-member-browse:disabled { opacity: 0.4; cursor: not-allowed; }
  .gc-actions { display: flex; gap: 6px; }
  .gc-access-btn {
    flex: 1; background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.1);
    color: inherit; padding: 4px 8px; border-radius: 4px; cursor: pointer; font-size: 11px;
  }
  .gc-access-btn:hover { background: rgba(255, 255, 255, 0.12); }
  .gc-all { color: #888; font-size: 10px; }
</style>
