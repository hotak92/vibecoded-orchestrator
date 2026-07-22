<script lang="ts">
  // Defect B (v0.2.68): ADAPTER from the `project-setup` store to the shared
  // presentational `OperationProgressBanner`. Mounted GLOBALLY in
  // +layout.svelte so it survives the post-add route change (ProjectSelector
  // closes its modal and the user navigates to the new project view; a
  // component-local banner would unmount).
  //
  // Builds the normalized view-model: plain-language phase label, prominent
  // project name, an alive/elapsed indicator, reassurance copy, the queue
  // count, the deferred-is-informational-amber distinction, and the failed →
  // Retry wiring. The store is the module-singleton that listens for
  // `project://setup-progress` and re-toasts terminal warnings (F5).

  import { onDestroy } from 'svelte';
  import { projectSetup } from '$lib/stores/project-setup';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import OperationProgressBanner from '$lib/components/OperationProgressBanner.svelte';
  import type { ProjectSetupPhase, ProjectSetupStatus } from '$lib/types/launcher';

  // 30s after a terminal state we auto-hide done/deferred (failed stays until
  // dismissed or retried). Tick a clock for the elapsed indicator + auto-hide.
  const HIDE_TERMINAL_AFTER_MS = 30_000;
  let now = $state(Date.now());
  let tick: ReturnType<typeof setInterval> | null = null;

  $effect(() => {
    const a = $projectSetup.active;
    const live = a && (a.status === 'running' || a.status === 'pending');
    // Run a 1Hz clock while a setup is live OR within the terminal-hide
    // window, so both the elapsed indicator and the auto-hide re-evaluate.
    const needTick =
      a !== null && (live || now - a.observed_at < HIDE_TERMINAL_AFTER_MS + 1000);
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

  function elapsedLabel(ms: number): string {
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return `${s}s elapsed`;
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}m ${rem}s elapsed`;
  }

  function phaseLabel(
    status: ProjectSetupStatus,
    phase: ProjectSetupPhase | null,
  ): string {
    if (status === 'pending') return 'Queued…';
    if (status === 'running') {
      switch (phase) {
        case 'bootstrap':
          return 'Creating knowledge collections…';
        case 'bundle':
          return 'Installing project bundle (hooks, scripts, agents)…';
        case 'post_bundle':
          return 'Indexing — continues in the background…';
        default:
          return 'Setting up…';
      }
    }
    if (status === 'deferred')
      return 'Knowledge collections will be created when Weaviate is ready.';
    if (status === 'done') return 'Setup complete.';
    return 'Setup failed.';
  }

  async function retry(projectId: string) {
    // Strict invoke: a failed retry must surface, not vanish. A silent
    // no-op leaves the banner stuck on "failed" with no explanation.
    try {
      await invoke<void>('retry_project_setup', { projectId });
    } catch (e) {
      toast.error(`Retry failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  // Build the view-model reactively. `now` is a $state so elapsed + auto-hide
  // re-evaluate each tick.
  let vm = $derived.by(() => {
    const a = $projectSetup.active;
    if (!a) return null;

    // Auto-hide done/deferred after the window (failed never auto-hides).
    if (
      (a.status === 'done' || a.status === 'deferred') &&
      now - a.observed_at >= HIDE_TERMINAL_AFTER_MS
    ) {
      return null;
    }

    const status: 'running' | 'deferred' | 'done' | 'failed' =
      a.status === 'pending' ? 'running' : a.status;

    // Title shows the project name prominently + the queue count when adds
    // are waiting behind this one ("Adding X — N queued").
    const queued = $projectSetup.queue.length;
    let title = `Setting up ${a.project_name}`;
    if (queued > 0) {
      title = `Adding ${a.project_name} — ${queued} queued`;
    }

    // Detail line: reassurance copy while running, elapsed timer, then the
    // appropriate terminal copy.
    let detail: string | undefined;
    if (a.status === 'running' || a.status === 'pending') {
      detail =
        `Project saved — setup (hooks, indexing) finishes in the background · ` +
        elapsedLabel(now - a.observed_at);
    } else if (a.status === 'deferred') {
      detail = 'Your project is ready to use now; the deferred work catches up automatically.';
    } else if (a.status === 'done') {
      detail = 'Hooks installed, knowledge collections ready.';
    }

    return {
      title,
      phaseLabel: phaseLabel(a.status, a.phase),
      status,
      detail,
      error: a.error,
      warnings: a.warnings,
      onRetry: a.status === 'failed' ? () => retry(a.project_id) : null,
      onDismiss:
        a.status === 'done' || a.status === 'deferred'
          ? () => projectSetup.dismiss()
          : null,
    };
  });
</script>

<OperationProgressBanner {vm} />
