<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // Gate `/coordination` on the cached Pro tier (v0.2.91, P2-B4).
  // Sibling of `routes/hub/+layout.svelte`; both are thin wrappers around
  // the shared deny screen. The enforcing half lives in
  // `commands/coordination.rs`, where all five commands re-check the tier.
  import ProRouteGate from '$lib/components/ProRouteGate.svelte';
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();
</script>

<ProRouteGate feature="Team coordination">
  {@render children()}
</ProRouteGate>
