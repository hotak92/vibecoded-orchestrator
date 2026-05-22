<script lang="ts">
  // StatusDisplayControl — read-only polled status card.
  //
  // v0.2.26 control kind: `status_display`.
  //
  // Flow:
  //   1. On mount, dispatch `source` (kick request).
  //      - If `source` is an ActionDescriptor with `polling` set, the
  //        Rust dispatcher's poller will fire `polling.progress_event`
  //        Tauri events on each tick; we subscribe and re-render.
  //      - Otherwise (legacy command or descriptor without polling),
  //        we fall back to an in-process re-poll at a default 10 s
  //        interval. The dispatcher returns the response directly, so
  //        we render once and then optionally re-fetch.
  //   2. `render_template` substitutes `{{field}}` placeholders from
  //      the response's top-level fields.
  //   3. On unmount, unsubscribe from events + clear any interval.
  //
  // We intentionally do NOT call `set_module_setting` — this control is
  // read-only output, not user state.

  import { onMount, onDestroy } from 'svelte';
  import { listen, tauriAvailable } from '$lib/tauri';
  import { dispatchAction, renderTemplate } from '$lib/module-dispatch';
  import { isActionDescriptor, type StatusDisplayControl } from '$lib/types/manifest';
  import { toast } from '$lib/stores/toast';

  let {
    control,
    moduleId,
    projectId,
    disabled = false,
  }: {
    control: StatusDisplayControl;
    moduleId: string;
    projectId: string;
    disabled?: boolean;
  } = $props();

  // Last response payload (top-level fields used by `render_template`).
  let payload = $state<Record<string, unknown> | null>(null);
  let loading = $state(true);
  let error = $state<string>('');

  // Cleanup callbacks set up in onMount. Captured here so onDestroy can
  // tear them down in any order without races.
  let unlistenProgress: (() => void) | null = null;
  let unlistenFailed: (() => void) | null = null;
  let fallbackTimer: ReturnType<typeof setInterval> | null = null;
  // Used to skip stale responses arriving after the component is gone.
  let isMounted = true;

  // Default polling interval when the action has no polling spec (i.e.
  // the source is a one-shot read but we still want fresh data). 10 s
  // is conservative; module authors can override by attaching a
  // polling spec to the source descriptor.
  const FALLBACK_POLL_MS = 10_000;

  // ─── Event payload matchers ────────────────────────────────────────
  //
  // The Rust dispatcher emits the configured `progress_event` /
  // `failed_event` (defaults: `module://action-progress`,
  // `module://action-failed`). Multiple status_displays on the same
  // page could subscribe to the same default event name and step on
  // each other. To prevent cross-control bleed we filter incoming
  // events by `control_id` if the payload carries one. Module authors
  // who care about isolation can give each polling spec a unique
  // `progress_event` (e.g. `module://rl-train-progress`).

  function matchesThisControl(payload: unknown): boolean {
    if (!payload || typeof payload !== 'object') return true; // permissive
    const p = payload as { control_id?: unknown; module_id?: unknown };
    // If the payload tags a control_id, require it to match.
    if (typeof p.control_id === 'string' && p.control_id !== control.id) {
      return false;
    }
    // If the payload tags a module_id, require it to match too.
    if (typeof p.module_id === 'string' && p.module_id !== moduleId) {
      return false;
    }
    return true;
  }

  async function fetchOnce() {
    error = '';
    try {
      const resp = await dispatchAction<unknown>(
        { moduleId, projectId },
        control.source,
        null,
      );
      if (!isMounted) return;
      if (resp && typeof resp === 'object') {
        payload = resp as Record<string, unknown>;
      } else {
        // Wrap a scalar response so the template still has something
        // to anchor on (e.g. `{{value}}`).
        payload = { value: resp };
      }
    } catch (err) {
      if (!isMounted) return;
      error = err instanceof Error ? err.message : String(err);
      // Don't toast on every failed poll — that would spam. Surface
      // the error inline instead.
    } finally {
      if (isMounted) loading = false;
    }
  }

  onMount(() => {
    if (!tauriAvailable() || !projectId) {
      loading = false;
      return;
    }

    // Decide whether the Rust poller will drive us (descriptor with
    // polling) or we self-poll on a fallback timer.
    const hasNativePolling =
      isActionDescriptor(control.source) && control.source.polling != null;

    void (async () => {
      // Kick the action.
      await fetchOnce();

      if (hasNativePolling && isActionDescriptor(control.source)) {
        const polling = control.source.polling!;
        const progressEvent = polling.progress_event ?? 'module://action-progress';
        const failedEvent = polling.failed_event ?? 'module://action-failed';

        unlistenProgress = await listen<Record<string, unknown>>(
          progressEvent,
          (e) => {
            if (!isMounted) return;
            if (!matchesThisControl(e.payload)) return;
            payload = e.payload as Record<string, unknown>;
            error = '';
          },
        );
        unlistenFailed = await listen<Record<string, unknown>>(
          failedEvent,
          (e) => {
            if (!isMounted) return;
            if (!matchesThisControl(e.payload)) return;
            const p = e.payload as { error?: unknown; message?: unknown };
            error =
              typeof p.error === 'string'
                ? p.error
                : typeof p.message === 'string'
                  ? p.message
                  : 'Action failed';
            toast.error(`${control.label}: ${error}`);
          },
        );
      } else {
        // Self-polling fallback.
        fallbackTimer = setInterval(() => {
          if (!isMounted) return;
          void fetchOnce();
        }, FALLBACK_POLL_MS);
      }
    })();
  });

  onDestroy(() => {
    isMounted = false;
    if (unlistenProgress) {
      try {
        unlistenProgress();
      } catch {
        /* noop — event bus already torn down */
      }
      unlistenProgress = null;
    }
    if (unlistenFailed) {
      try {
        unlistenFailed();
      } catch {
        /* noop */
      }
      unlistenFailed = null;
    }
    if (fallbackTimer !== null) {
      clearInterval(fallbackTimer);
      fallbackTimer = null;
    }
  });

  const rendered = $derived(renderTemplate(control.render_template, payload));
</script>

<div class="status-display-control" class:disabled>
  <div class="control-label-row">
    <span class="control-label">{control.label}</span>
    <span
      class="tooltip-affordance"
      title={control.tooltip ?? control.label}
      aria-label="More info"
    >?</span>
  </div>
  <div
    class="status-card"
    class:error={error !== ''}
    role="status"
    aria-live="polite"
  >
    {#if loading}
      <span class="status-skeleton" aria-label="Loading">Loading…</span>
    {:else if error}
      <span class="status-text status-error">{error}</span>
    {:else}
      <span class="status-text">{rendered}</span>
    {/if}
  </div>
</div>

<style>
  .status-display-control {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .status-display-control.disabled {
    opacity: 0.5;
  }

  .control-label-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .control-label {
    font-size: 13px;
    font-weight: 500;
  }

  .tooltip-affordance {
    display: inline-flex;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.08);
    color: var(--color-muted);
    font-size: 10px;
    font-weight: 700;
    cursor: help;
    flex-shrink: 0;
  }
  .tooltip-affordance:hover {
    background: rgba(255, 255, 255, 0.16);
    color: var(--color-text);
  }

  .status-card {
    padding: 10px 14px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    font-size: 13px;
    font-family: var(--font-mono, ui-monospace, 'SF Mono', Menlo, monospace);
    color: var(--color-text);
    min-height: 1.6em;
  }
  .status-card.error {
    background: rgba(231, 76, 60, 0.08);
    border-color: rgba(231, 76, 60, 0.30);
  }

  .status-skeleton {
    color: var(--color-muted);
    font-style: italic;
  }

  .status-text {
    white-space: pre-wrap;
    word-break: break-word;
  }
  .status-text.status-error {
    color: #e74c3c;
  }
</style>
