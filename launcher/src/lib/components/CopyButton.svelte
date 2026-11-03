<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // v0.2.105 (L-SMALL) — the ONE Copy button for the launcher.
  //
  // Extracted from `NotificationBell.svelte` so the notification inbox and the
  // deferral-ledger rows (the project's "pending actions" panel and the
  // Updates page's Pending Actions panel) share one clipboard routine and one
  // look. The write itself lives in `$lib/clipboard.ts`; this file is the
  // button around it.
  //
  // Styled with the brand tokens (`--color-mid` / `--color-text`) so it reads
  // the same on the navy panel and the navy bell popover.

  import { copyToClipboard } from '$lib/clipboard';

  let {
    text,
    label = 'Copy',
    title = 'Copy',
  }: { text: string; label?: string; title?: string } = $props();
</script>

<button
  class="copy-icon-btn"
  data-testid="copy-btn"
  onclick={() => void copyToClipboard(text)}
  aria-label={label}
  {title}
>
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
</button>

<style>
  .copy-icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    flex-shrink: 0;
    border-radius: 6px;
    border: none;
    background: none;
    color: var(--color-mid, #94a3b8);
    cursor: pointer;
    transition: background 0.15s ease, color 0.15s ease;
  }
  .copy-icon-btn:hover {
    background: rgba(255, 255, 255, 0.1);
    color: var(--color-text, #f1f5f9);
  }
</style>