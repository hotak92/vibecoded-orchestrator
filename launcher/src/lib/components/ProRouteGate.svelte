<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // ProRouteGate — client-side deny screen for the `proOnly` routes.
  //
  // v0.2.91 (P2-B4 / plan decision #28). Mirrors
  // `routes/admin/+layout.svelte`'s in-house pattern: render a named
  // placeholder instead of the feature when the cached orchestrator tier
  // doesn't clear the floor, and say plainly that the client check is a
  // courtesy — the data behind it is gated server-side.
  //
  // Factored into a component rather than copy-pasted into each layout:
  // `/coordination` and `/hub` are BOTH proOnly, and a third would make
  // three copies of the same markup + style block. The layouts stay five
  // lines each.
  //
  // The authority is `dashboard::require_tier`, which every command on
  // those routes now calls (`commands/coordination.rs`,
  // `commands/hub_proxy.rs`). Patching this component reveals the page
  // chrome; every invoke it makes still comes back refused.
  import { license } from '$lib/stores/license';
  import { hasProTier } from '$lib/license-gate';
  import type { Snippet } from 'svelte';

  let {
    feature,
    children,
  }: {
    /** Noun phrase naming the gated surface, e.g. "Team coordination". */
    feature: string;
    children: Snippet;
  } = $props();

  const unlocked = $derived(hasProTier($license.cache?.orchestrator_tier));
</script>

{#if unlocked}
  {@render children()}
{:else}
  <div class="pro-deny">
    <h2>{feature} is a Pro feature</h2>
    <p>
      This route is reserved for users with a Pro (or higher) orchestrator
      license. Server-side tier classification gates the data behind it;
      client-side bypass gives you nothing.
    </p>
    <p>
      Already licensed? Open the License Manager from the menu bar and run a
      refresh, then check that <code>orchestrator_tier</code> in the license
      cache reads <code>pro</code> or above.
    </p>
    <a class="pro-deny-cta" href="/store">See what Pro includes</a>
  </div>
{/if}

<style>
  .pro-deny {
    margin: 40px auto;
    max-width: 640px;
    padding: 24px 28px;
    border: 1px solid rgba(var(--color-purple-rgb), 0.3);
    background: rgba(var(--color-purple-rgb), 0.05);
    border-radius: var(--radius-card);
    color: var(--color-text);
  }
  .pro-deny h2 {
    margin: 0 0 12px;
    color: var(--color-purple);
    font-size: 18px;
  }
  .pro-deny p {
    line-height: 1.5;
    font-size: 13px;
    color: var(--color-mid);
  }
  .pro-deny code {
    font-family: ui-monospace, monospace;
    font-size: 12px;
    color: var(--color-purple);
    background: rgba(255, 255, 255, 0.04);
    padding: 1px 5px;
    border-radius: 3px;
  }
  .pro-deny-cta {
    display: inline-block;
    margin-top: 8px;
    font-size: 13px;
    color: var(--color-teal);
    text-decoration: none;
  }
  .pro-deny-cta:hover {
    color: var(--color-teal-hover);
    text-decoration: underline;
  }
</style>
