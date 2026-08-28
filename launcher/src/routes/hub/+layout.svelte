<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // Gate `/hub` on the cached Pro tier (v0.2.91, P2-B4). Sibling of
  // `routes/coordination/+layout.svelte`. The enforcing half lives in
  // `commands/hub_proxy.rs`, where the four commands this route invokes
  // re-check the tier (the Preferences boot-autostart pair stays ungated).
  import ProRouteGate from '$lib/components/ProRouteGate.svelte';
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();
</script>

<ProRouteGate feature="The Orchestrator Hub">
  {@render children()}
</ProRouteGate>
