<script lang="ts">
  import ModuleCatalog from '$lib/components/ModuleCatalog.svelte';
  import { ui } from '$lib/stores/ui';
</script>

<svelte:head>
  <title>Modules — VCT Launcher</title>
</svelte:head>

<div class="page">
  <ModuleCatalog
    onOpenActivation={() => ui.openActivation()}
    onOpenSettings={() => ui.openSettings('secrets')}
  />
</div>

<style>
  .page {
    height: 100%;
    overflow-y: auto;
    /* Match the home page's content padding (28px 32px). Previously the
       catalog rendered flush against the sidebar/top edges because
       neither this wrapper nor ModuleCatalog's `.catalog` root carried
       any padding. ModuleCatalog is a shared component, so the padding
       lives here on the route wrapper rather than in the component. */
    padding: 28px 32px;
    box-sizing: border-box;
  }
</style>
