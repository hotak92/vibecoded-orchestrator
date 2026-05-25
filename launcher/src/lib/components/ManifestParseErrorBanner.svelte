<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // ManifestParseErrorBanner — L9 aggregated banner for catalog parse
  // failures.
  //
  // v0.2.33 (Agent E, 2026-05-25): when `CatalogResponse.parse_errors`
  // is non-empty, the Modules tab header renders this banner. Clicking
  // opens `ManifestParseErrorModal` with the full list. The banner uses
  // an aggregated count so MULTIPLE failures still surface as one
  // affordance instead of N stacked banners (per architecture review
  // §10.a).
  //
  // Pre-v0.2.33 parse failures only landed in stderr; the user saw
  // the catalog tile fall back to the builtin placeholder with no
  // hint that anything was wrong. This banner removes the silent
  // fall-back: a user with a malformed manifest now sees the issue
  // immediately, with the underlying error one click away.
  //
  // Logging: the parent component (`ModuleCatalog.svelte`) calls the
  // `log_manifest_parse_errors` Tauri command in lock-step with the
  // banner mount so a postmortem JSONL entry exists at
  // `<install>/state/logs/launcher_errors.jsonl` (or
  // `~/.vct/launcher_errors.jsonl` when no install root is resolvable).
  //
  // Design note: this is purely a count + click affordance. The list +
  // per-error detail render belongs to `ManifestParseErrorModal` so
  // the banner stays lightweight (frequent renders, narrow footprint).

  import type { ManifestParseError } from '$lib/types/launcher';

  let {
    errors,
    onOpen,
  }: {
    errors: ManifestParseError[];
    onOpen: () => void;
  } = $props();
</script>

{#if errors.length > 0}
  <button
    type="button"
    class="parse-error-banner"
    onclick={onOpen}
    aria-label={`${errors.length} module manifest${errors.length === 1 ? '' : 's'} couldn't be parsed — click for details`}
  >
    <span class="icon" aria-hidden="true">⚠</span>
    <span class="message">
      {errors.length}
      module manifest{errors.length === 1 ? '' : 's'}
      couldn't be parsed.
      <span class="cta">Click for details.</span>
    </span>
  </button>
{/if}

<style>
  .parse-error-banner {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    margin: 0 0 16px 0;
    padding: 10px 14px;
    background: rgba(241, 196, 15, 0.10);
    border: 1px solid rgba(241, 196, 15, 0.30);
    border-radius: 10px;
    color: #f1c40f;
    font-size: 13px;
    font-family: inherit;
    text-align: left;
    cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
  }
  .parse-error-banner:hover {
    background: rgba(241, 196, 15, 0.16);
    border-color: rgba(241, 196, 15, 0.45);
  }
  .parse-error-banner:focus-visible {
    outline: 2px solid rgba(241, 196, 15, 0.7);
    outline-offset: 1px;
  }
  .icon {
    font-size: 16px;
    line-height: 1;
    flex-shrink: 0;
  }
  .message {
    flex: 1;
    line-height: 1.4;
  }
  .cta {
    margin-left: 6px;
    font-weight: 600;
    text-decoration: underline;
  }
</style>
