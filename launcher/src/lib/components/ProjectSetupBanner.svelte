<script lang="ts">
  // Defect B (v0.2.68): ADAPTER from the `project-setup` store to the shared
  // presentational `OperationProgressBanner`. Mounted GLOBALLY in
  // +layout.svelte — in the banner stack directly BELOW the MenuBar header —
  // so it survives the post-add route change (ProjectSelector closes its
  // modal and the user navigates to the new project view; a component-local
  // banner would unmount).
  //
  // v0.2.95 R7 (field report, repeat): it used to be mounted ABOVE the
  // MenuBar, which pinned it to the very top edge of the window — a strip
  // glued to the titlebar, above all chrome and detached from the content.
  // It now renders in the shell's below-header banner stack, in the same
  // visual family as the KG-sync / code-graph banners. Deliberately NOT a
  // blocking modal: the operation runs in the background and the user is
  // free to keep working.
  //
  // The store is the module-singleton that listens for
  // `project://setup-progress` and re-toasts terminal warnings (F5). The
  // view-model itself (stage labels, elapsed, auto-hide window, which
  // affordance each state gets) lives in `project-setup-banner-logic.ts`,
  // where it is unit-tested.

  import { onDestroy } from 'svelte';
  import { projectSetup } from '$lib/stores/project-setup';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import OperationProgressBanner from '$lib/components/OperationProgressBanner.svelte';
  import {
    buildSetupBannerView,
    setupBannerNeedsTick,
  } from '$lib/components/project-setup-banner-logic';

  // Tick a 1Hz clock for the elapsed indicator + the terminal auto-hide.
  let now = $state(Date.now());
  let tick: ReturnType<typeof setInterval> | null = null;

  $effect(() => {
    const needTick = setupBannerNeedsTick($projectSetup.active, now);
    if (needTick && tick === null) {
      tick = setInterval(() => (now = Date.now()), 1000);
    } else if (!needTick && tick !== null) {
      clearInterval(tick);
      tick = null;
    }
  });

  onDestroy(() => {
    if (tick !== null) clearInterval(tick);
  });

  async function retry(projectId: string) {
    // Strict invoke: a failed retry must surface, not vanish. A silent
    // no-op leaves the banner stuck on "failed" with no explanation.
    try {
      await invoke<void>('retry_project_setup', { projectId });
    } catch (e) {
      toast.error(`Retry failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  // `now` is a $state so elapsed + auto-hide re-evaluate each tick.
  let vm = $derived.by(() => {
    const a = $projectSetup.active;
    const view = buildSetupBannerView(a, $projectSetup.queue.length, now);
    if (!view || !a) return null;
    return {
      ...view,
      onRetry: view.canRetry ? () => retry(a.project_id) : null,
      onDismiss: view.canDismiss ? () => projectSetup.dismiss() : null,
    };
  });
</script>

<OperationProgressBanner {vm} />
