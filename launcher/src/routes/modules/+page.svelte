<script lang="ts">
  import ModuleCatalog from '$lib/components/ModuleCatalog.svelte';
  import { ui } from '$lib/stores/ui';
</script>

<div class="page">
  <ModuleCatalog
    onOpenActivation={() => ui.openActivation()}
    onOpenSettings={() => ui.openSettings()}
  />
</div>

<style>
  .page {
    height: 100%;
    overflow-y: auto;
  }
</style>
