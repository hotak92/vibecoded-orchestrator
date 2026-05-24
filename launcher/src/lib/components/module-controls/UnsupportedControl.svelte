<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // UnsupportedControl — placeholder for control kinds this launcher
  // version doesn't recognise.
  //
  // v0.2.33 (Agent D, 2026-05-25): the Rust manifest schema's
  // `ConfigControl::Unsupported` variant lands here when a paid
  // module ships with a control kind unknown to this launcher
  // version (e.g. a future `file_drop_zone` shipped in v0.3.0 that
  // a v0.2.33 user hasn't yet updated to render).
  //
  // The placeholder explains what's happening + invites the user to
  // update. Other controls in the same section render normally —
  // lenient parse means the unknown control doesn't poison the rest
  // of the tab.
  //
  // Design note: we intentionally show the `kind_string` so users
  // pinging support can quote the missing kind by name. The raw
  // payload's `label` (if present) is also surfaced so the user
  // sees the same affordance label the module author intended,
  // even when this launcher can't render the actual control.

  import type { UnsupportedControl } from '$lib/types/manifest';

  let {
    control,
  }: {
    control: UnsupportedControl;
  } = $props();

  // Best-effort: pull a human-friendly label out of the raw payload
  // if the unknown control had one. Falls back to the kind_string.
  const displayLabel = $derived(
    typeof control.raw?.label === 'string'
      ? (control.raw.label as string)
      : control.kind_string,
  );
  const controlId = $derived(
    typeof control.raw?.id === 'string' ? (control.raw.id as string) : '',
  );
</script>

<div
  class="unsupported-control"
  role="status"
  aria-live="polite"
  data-kind={control.kind_string}
  data-control-id={controlId}
>
  <span class="badge" aria-hidden="true">!</span>
  <div class="body">
    <p class="title">
      <span class="label">{displayLabel}</span>
      <span class="kind">(kind: <code>{control.kind_string}</code>)</span>
    </p>
    <p class="hint">
      This control requires a newer launcher version. Update your launcher
      to render it; other controls in this section still work.
    </p>
  </div>
</div>

<style>
  .unsupported-control {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 12px;
    border: 1px dashed rgba(255, 196, 0, 0.35);
    border-radius: 6px;
    background: rgba(255, 196, 0, 0.05);
  }
  .badge {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: rgba(255, 196, 0, 0.2);
    color: #ffc400;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 13px;
  }
  .body {
    flex: 1;
    min-width: 0;
  }
  .title {
    margin: 0 0 4px 0;
    font-size: 13px;
    color: var(--color-text, #e5e5e5);
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: baseline;
  }
  .label {
    font-weight: 600;
  }
  .kind {
    font-size: 12px;
    color: var(--color-muted, #9a9a9a);
  }
  .kind code {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 11px;
    padding: 1px 4px;
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.06);
  }
  .hint {
    margin: 0;
    font-size: 12px;
    color: var(--color-muted, #9a9a9a);
    line-height: 1.4;
  }
</style>
