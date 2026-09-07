<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  //
  // v0.2.92 WP-D (GUI half) — per-project bundle-staleness chip.
  //
  // Thin renderer over `$lib/bundle-staleness`: all gating lives in those
  // pure functions so vitest can pin it without mounting Svelte (same
  // split as `project-folder-health.ts` / ProjectCard's warning banner).
  //
  // Three states, three visually and textually distinct treatments:
  //
  //   current — teal (#00BFA6), the brand's "healthy" token.
  //   stale   — amber, an ACTIONABLE state ("Update all" fixes it).
  //   unknown — grey with a dashed border and a "?" glyph. Deliberately
  //             NOT amber: an undetermined project is not one the remedy
  //             applies to (you cannot bundle-update a folder that is
  //             gone), and it is emphatically not the teal "fine" state.
  //             The dashed border is the non-colour cue, so the three
  //             states stay distinguishable without relying on hue.
  //
  // The chip is informational only — it never triggers anything. The
  // remedy is the user choosing "Update all".

  import { chipFor } from '$lib/bundle-staleness';
  import type { BundleStalenessCensus } from '$lib/types/launcher';

  interface Props {
    /** Last census result, or null while loading / when unavailable. */
    census: BundleStalenessCensus | null;
    projectId: string;
  }

  let { census, projectId }: Props = $props();

  const chip = $derived(chipFor(census, projectId));
  const glyph = $derived(
    chip.verdict === 'current' ? '✓' : chip.verdict === 'stale' ? '⟳' : '?',
  );
</script>

<span
  class="bsc"
  class:ok={chip.tone === 'ok'}
  class:warn={chip.tone === 'warn'}
  class:unknown={chip.tone === 'unknown'}
  title={chip.detail}
  data-testid="bundle-staleness-chip"
  data-verdict={chip.verdict}
  data-reason={chip.reason}
>
  <span class="bsc-glyph" aria-hidden="true">{glyph}</span>
  <span class="bsc-label">{chip.label}</span>
</span>

<style>
  .bsc {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.03em;
    border: 1px solid transparent;
    white-space: nowrap;
  }
  .bsc-glyph { font-size: 11px; line-height: 1; }
  /* Healthy — brand teal. */
  .bsc.ok {
    background: rgba(0, 191, 166, 0.12);
    border-color: rgba(0, 191, 166, 0.3);
    color: rgb(0, 191, 166);
  }
  /* Actionable — amber. "Update all" clears this population. */
  .bsc.warn {
    background: rgba(255, 176, 32, 0.12);
    border-color: rgba(255, 176, 32, 0.35);
    color: rgb(255, 190, 80);
  }
  /* Undetermined — grey + DASHED border. The dashed edge is the
     non-colour differentiator so the third state is distinguishable
     from the other two without relying on hue alone. */
  .bsc.unknown {
    background: rgba(255, 255, 255, 0.04);
    border-color: rgba(255, 255, 255, 0.28);
    border-style: dashed;
    color: #b3b3b3;
    font-style: italic;
  }
</style>
