<script lang="ts">
  // v0.2.95 R7 — THE shared status-banner shell.
  //
  // Before this extraction the launcher shipped FOUR verbatim clones of the
  // same banner (KgSyncBanner, KgSummaryBanner, CodeGraphBuildBanner,
  // OperationProgressBanner): identical markup skeleton + ~150 identical
  // lines of CSS each, differing only in labels, glyphs and the event they
  // listen to. Their own comments admitted the cloning ("Styles cloned
  // verbatim from CodeGraphBuildBanner.svelte"). One concern, one home: the
  // chrome lives here, each banner keeps only its own data + actions.
  //
  // PRESENTATIONAL ONLY — no store coupling, no visibility policy. Callers
  // decide when to mount it and pass a normalized view.
  //
  // Caller-authored content goes through two snippets:
  //   `actions`         — the right-hand buttons (each banner has its own
  //                       verbs: Retry sync / Rebuild / Drop & rebuild…).
  //   `expandedContent` — the inline failure/detail drawer.
  // Snippets are compiled in the CALLER, so the classes they use
  // (`.bg-btn-primary`, `.bg-pre`, …) are styled from here via `:global()`
  // nested under `.bg-banner` — that keeps the banner vocabulary in one
  // file without leaking the rules to unrelated components.
  //
  // Palette + motion per `.claude/references/VCO_BRAND_REFERENCE.md`
  // (navy surface, teal #00BFA6 primary, pink #FF4FA0 failure). Values are
  // carried over verbatim from the pre-extraction banners.

  import type { Snippet } from 'svelte';
  import type { BannerTone } from './status-banner-tone';

  interface Props {
    /** Visual tone — map a domain status through `status-banner-tone.ts`. */
    tone: BannerTone;
    /** Single-character status glyph (⟳ ✓ ! ⚠ ∅ ·). */
    glyph: string;
    /** Rotate the glyph — live work, or a caller's invoke in flight. */
    spinning?: boolean;
    /** Headline ("KG sync: queued", "Setting up <project>"). */
    title: string;
    /** Render the headline at 700 weight — used when the headline carries a
     *  project NAME the user is waiting on, rather than a job label. */
    strongTitle?: boolean;
    /** Optional second line: plain-language phase. */
    phase?: string | null;
    /** Optional last line: counts, elapsed, reassurance copy. */
    detail?: string | null;
    /** `role="alert"` instead of `role="status"` — failures only. */
    alert?: boolean;
    /** Right-hand action buttons. */
    actions?: Snippet;
    /** Inline detail drawer, rendered under the row when `showExpanded`. */
    expandedContent?: Snippet;
    showExpanded?: boolean;
    /** Accessible name of the drawer. */
    expandedLabel?: string;
    /** Drawer role — the sync banners use `dialog`, the operation banner
     *  `group` (it lists warnings rather than a failure). */
    expandedRole?: 'dialog' | 'group';
  }

  let {
    tone,
    glyph,
    spinning = false,
    title,
    strongTitle = false,
    phase = null,
    detail = null,
    alert = false,
    actions,
    expandedContent,
    showExpanded = false,
    expandedLabel = 'Details',
    expandedRole = 'dialog',
  }: Props = $props();
</script>

<div
  class="bg-banner tone-{tone}"
  role={alert ? 'alert' : 'status'}
  aria-live="polite"
>
  <div class="bg-row">
    <span class="bg-glyph" class:spin={spinning} aria-hidden="true">{glyph}</span>
    <div class="bg-text">
      <div class="bg-label" class:strong={strongTitle}>{title}</div>
      {#if phase}
        <div class="bg-phase">{phase}</div>
      {/if}
      {#if detail}
        <div class="bg-detail">{detail}</div>
      {/if}
    </div>
    <div class="bg-actions">
      {@render actions?.()}
    </div>
  </div>

  {#if showExpanded && expandedContent}
    <div class="bg-expand" role={expandedRole} aria-label={expandedLabel}>
      {@render expandedContent()}
    </div>
  {/if}
</div>

<style>
  /* Every rule below was previously duplicated in four components; values
     are theirs, verbatim. Do NOT re-inline a copy into a banner — add a
     prop here instead. */
  .bg-banner {
    display: block;
    border-bottom: 1px solid transparent;
    font-size: 13px;
    line-height: 1.4;
  }
  .bg-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 24px;
  }
  .bg-glyph {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    font-family: ui-monospace, monospace;
    font-size: 12px;
    font-weight: 600;
    flex-shrink: 0;
  }
  .bg-glyph.spin { animation: bg-spin 1.4s linear infinite; }
  @keyframes bg-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  /* Honour the OS reduced-motion setting, like the global `.bg-glyph-spin`
     button glyph in app.css already does. */
  @media (prefers-reduced-motion: reduce) {
    .bg-glyph.spin { animation: none; }
  }
  .bg-text { flex: 1; min-width: 0; }
  .bg-label { font-weight: 600; }
  .bg-label.strong { font-weight: 700; }
  .bg-phase { font-size: 12.5px; margin-top: 1px; }
  .bg-detail { font-size: 12px; color: rgba(255,255,255,0.55); margin-top: 2px; }
  .bg-actions {
    display: flex;
    gap: 8px;
    flex-shrink: 0;
    align-items: center;
  }

  /* ── Caller-authored content (snippets compile in the caller, so these
        must be :global, anchored under our own root class). ── */
  .bg-banner :global(.bg-btn-secondary),
  .bg-banner :global(.bg-btn-primary) {
    padding: 4px 12px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
    font-family: inherit;
  }
  .bg-banner :global(.bg-btn-secondary) {
    background: rgba(255,255,255,0.06);
    color: #ccc;
    border-color: rgba(255,255,255,0.12);
  }
  .bg-banner :global(.bg-btn-secondary:hover) { background: rgba(255,255,255,0.1); }
  .bg-banner :global(.bg-btn-primary) {
    background: rgb(0,191,166);
    color: #001a17;
  }
  .bg-banner :global(.bg-btn-primary:hover:not(:disabled)) { background: rgb(0,210,180); }
  .bg-banner :global(.bg-btn-primary:disabled) { opacity: 0.5; cursor: default; }
  .bg-banner :global(.bg-btn-x) {
    background: none; border: none; color: inherit;
    font-size: 18px; line-height: 1; cursor: pointer;
    padding: 0 8px; border-radius: 6px;
    opacity: 0.6;
  }
  .bg-banner :global(.bg-btn-x:hover) { opacity: 1; background: rgba(255,255,255,0.06); }

  .bg-expand {
    padding: 8px 24px 14px 56px;
    font-size: 12px;
    border-top: 1px dashed rgba(255,255,255,0.08);
  }
  .bg-expand :global(.bg-expand-row) { margin-bottom: 8px; }
  .bg-expand :global(.bg-expand-row strong) {
    display: block;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: rgba(255,255,255,0.5);
    margin-bottom: 4px;
  }
  .bg-expand :global(.bg-pre) {
    margin: 0;
    padding: 6px 8px;
    background: rgba(255,255,255,0.04);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 11px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 220px;
    overflow-y: auto;
    color: rgba(255,255,255,0.85);
  }

  /* ── Tones. One palette for every banner in the launcher. ── */
  .tone-pending {
    background: rgba(255,255,255,0.04);
    border-bottom-color: rgba(255,255,255,0.10);
    color: var(--color-mid, #999);
  }
  .tone-pending .bg-glyph {
    background: rgba(255,255,255,0.06);
    color: #999;
  }
  .tone-running {
    background: rgba(0,191,166,0.08);
    border-bottom-color: rgba(0,191,166,0.30);
    color: rgb(0,191,166);
  }
  .tone-running .bg-glyph {
    background: rgba(0,191,166,0.15);
    color: rgb(0,191,166);
  }
  .tone-success {
    background: rgba(70, 200, 120, 0.08);
    border-bottom-color: rgba(70, 200, 120, 0.30);
    color: rgb(120, 220, 160);
  }
  .tone-success .bg-glyph {
    background: rgba(70, 200, 120, 0.18);
    color: rgb(120, 220, 160);
  }
  /* Amber: informational — skipped work, a deferral, a partial prune. */
  .tone-warning {
    background: rgba(245, 179, 66, 0.08);
    border-bottom-color: rgba(245, 179, 66, 0.30);
    color: rgb(245, 179, 66);
  }
  .tone-warning .bg-glyph {
    background: rgba(245, 179, 66, 0.18);
    color: rgb(245, 179, 66);
  }
  .tone-failed {
    background: rgba(255, 79, 160, 0.10);
    border-bottom-color: rgba(255, 79, 160, 0.35);
    color: rgb(255, 130, 180);
  }
  .tone-failed .bg-glyph {
    background: rgba(255, 79, 160, 0.18);
    color: rgb(255, 130, 180);
  }
</style>
