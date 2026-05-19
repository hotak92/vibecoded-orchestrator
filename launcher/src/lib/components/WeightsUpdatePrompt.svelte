<script lang="ts">
  // Phase 4A (v0.2.21) — RL Reranker "weights update available" prompt.
  //
  // Listens for three Tauri events emitted by Stream B's launcher-side
  // poller / rotator:
  //
  //   - `module://weights-update-available` — a newer global model exists
  //     for this project's active RL module. Renders a modal asking the
  //     user how to apply it.
  //   - `module://finetune-progress`         — periodic progress updates
  //     while a background fine-tune is running (state ∈ running|done|
  //     failed). When state==='done', the modal auto-dismisses after 2s.
  //   - `module://finetune-failed`           — terminal-failure variant.
  //     The new global weights ARE applied (per Stream B contract), but
  //     the project-specific specialization failed. Surface the reason
  //     inline so the user knows their data didn't shape the model.
  //
  // On choice, invokes the `apply_weights_update` Tauri command with the
  // full original event payload (Stream B owns the command; we just call
  // it). Three choices, three semantics:
  //
  //   - 'now'    : download + run offline fine-tune. The modal stays open
  //                showing the progress bar; auto-dismisses on success.
  //   - 'skip'   : download + activate the new global weights as-is, no
  //                specialization. Modal closes immediately.
  //   - 'later'  : postpone (soft noop server-side). Modal closes.
  //
  // Security notes:
  //   - Release notes are rendered as PLAIN TEXT via `<pre>{notes}</pre>`.
  //     Defense in depth even though notes come from a private DB — never
  //     reach for `@html` here.
  //   - `embedding_source` is rendered as a label only; the component has
  //     no source-specific branching, so adding a new embedding source
  //     downstream works without UI changes.
  //
  // UX policy for v0.2.21:
  //   - REPLACE-on-new-event (not queue). If a second update event arrives
  //     while the modal is open, the newer one supersedes the older one.
  //     Rationale: a newer model is strictly better information; queueing
  //     stale updates is user-hostile.
  //   - All three buttons carry tooltips so power users can hover for the
  //     exact behaviour without clicking. See `title` attrs below.
  //   - Finetune failure does NOT auto-dismiss — the user has to actively
  //     acknowledge it (the new weights are live but unspecialized).
  import { onMount, onDestroy } from 'svelte';
  import { invoke, listen } from '$lib/tauri';
  import DialogRoot from '$lib/components/DialogRoot.svelte';

  // Locked wire contracts (Stream B owns the emitter side):
  //   module://weights-update-available
  interface WeightsUpdatePayload {
    project_id: string;
    module_id: string;
    latest_version: string;
    embedding_source: string;
    released_at: string;             // ISO-8601
    notes: string;                   // ≤500 chars (markdown, rendered as text)
    download_url: string;            // signed URL
    download_url_expires_at: string; // ISO-8601
    sha256: string;
  }

  //   module://finetune-progress
  interface FinetuneProgressPayload {
    project_id: string;
    module_id: string;
    percent: number;                          // 0..100
    message: string;
    state: 'running' | 'done' | 'failed';
  }

  //   module://finetune-failed
  interface FinetuneFailedPayload {
    project_id: string;
    module_id: string;
    reason: string;
  }

  type Choice = 'now' | 'skip' | 'later';

  let current = $state<WeightsUpdatePayload | null>(null);
  let progress = $state<FinetuneProgressPayload | null>(null);
  let error = $state<string | null>(null);
  let busy = $state(false);

  // Auto-dismiss timer handle — cleared on unmount or on re-trigger to
  // prevent stale callbacks from clearing a freshly-shown modal.
  let dismissTimer: ReturnType<typeof setTimeout> | null = null;

  // Unlisten functions for the three event subscriptions. Stored so
  // onDestroy can release them without re-awaiting the promises.
  let unlistenUpdate: (() => void) | null = null;
  let unlistenProgress: (() => void) | null = null;
  let unlistenFailed: (() => void) | null = null;

  function clearDismissTimer() {
    if (dismissTimer !== null) {
      clearTimeout(dismissTimer);
      dismissTimer = null;
    }
  }

  function reset() {
    clearDismissTimer();
    current = null;
    progress = null;
    error = null;
    busy = false;
  }

  function formatReleasedAt(iso: string): string {
    if (!iso) return '';
    try {
      return new Date(iso).toLocaleDateString();
    } catch {
      return iso;
    }
  }

  // Filter events to the currently-shown update — different project, or
  // different module on the same project, can also be emitting progress
  // we don't care about (e.g. a background fine-tune for a different
  // module finishing while THIS modal is open for a fresh prompt).
  function matchesCurrent(p: { project_id: string; module_id: string }): boolean {
    return (
      current !== null &&
      current.project_id === p.project_id &&
      current.module_id === p.module_id
    );
  }

  async function choose(choice: Choice) {
    if (!current || busy) return;
    busy = true;
    error = null;
    try {
      await invoke('apply_weights_update', {
        projectId: current.project_id,
        choice,
        response: current,
      });
      if (choice === 'now') {
        // Seed a "0%, starting…" progress state so the user sees the bar
        // immediately, instead of staring at frozen buttons until the
        // first progress event arrives.
        progress = {
          project_id: current.project_id,
          module_id: current.module_id,
          percent: 0,
          message: 'Starting…',
          state: 'running',
        };
      } else {
        // 'skip' (apply unmodified) and 'later' (defer) close instantly.
        reset();
      }
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  function dismiss() {
    reset();
  }

  onMount(() => {
    // listen() is async (dynamic-imports @tauri-apps/api/event), so we
    // kick off the three subscriptions in parallel and stash the unlisten
    // callbacks once each Promise resolves. The component is robust to
    // events that arrive before unlisten is wired up — the listener fires
    // either way; we only need unlisten for clean teardown.
    void (async () => {
      unlistenUpdate = await listen<WeightsUpdatePayload>(
        'module://weights-update-available',
        (e) => {
          // Replace-on-new-event policy: any in-flight progress / error
          // for an older event is dropped in favour of the fresher one.
          clearDismissTimer();
          current = e.payload;
          progress = null;
          error = null;
        },
      );

      unlistenProgress = await listen<FinetuneProgressPayload>(
        'module://finetune-progress',
        (e) => {
          if (!matchesCurrent(e.payload)) return;
          progress = e.payload;
          if (e.payload.state === 'done') {
            // Auto-dismiss the success state after 2s so the user sees
            // the completed bar before it disappears.
            clearDismissTimer();
            dismissTimer = setTimeout(() => reset(), 2000);
          }
          // state === 'failed' on the progress channel is handled by the
          // dedicated `finetune-failed` listener below (Stream B emits
          // both for a failure: a terminal progress event + a failed
          // event with the reason).
        },
      );

      unlistenFailed = await listen<FinetuneFailedPayload>(
        'module://finetune-failed',
        (e) => {
          if (!matchesCurrent(e.payload)) return;
          // Show the failure reason inline. The new global weights are
          // already live (per Stream B contract: failure happens AFTER
          // rotation), so phrase the message as "specialization failed,
          // unmodified weights are active". User has to dismiss.
          clearDismissTimer();
          progress = null;
          error =
            `Fine-tune failed: ${e.payload.reason}. ` +
            `The new global weights were applied without specialization.`;
        },
      );
    })();
  });

  onDestroy(() => {
    clearDismissTimer();
    unlistenUpdate?.();
    unlistenProgress?.();
    unlistenFailed?.();
  });

  // Per-button tooltip copy. Hoisted so the markup stays readable and the
  // copy lives next to the spec it implements.
  const TIP_NOW =
    'Downloads the new weights, runs an offline pass on your last ' +
    '30 days of events to re-specialize, then activates. ~30s.';
  const TIP_SKIP =
    'Downloads + activates the new global weights as-is. No specialization.';
  const TIP_LATER =
    'Postpones the prompt until the next poll. The new weights are not downloaded.';
</script>

{#if current}
  <DialogRoot open={true} width="520px" onClose={() => dismiss()}>
    {#snippet header()}
      <div class="header">
        <h2>RL Reranker — new global model available</h2>
        <span class="badge">Pro</span>
      </div>
    {/snippet}
    {#snippet body()}
      <!-- Re-narrow `current` inside the snippet scope. Svelte 5 snippets
           don't carry the outer {#if current} narrowing into their
           closure, so we alias it to a const that TS can prove non-null. -->
      {@const cur = current!}
      <div class="body">
        <div class="meta">
          <div>
            <strong>Version:</strong>
            {cur.latest_version}
          </div>
          <div>
            <strong>Embedding:</strong>
            {cur.embedding_source}
          </div>
          {#if cur.released_at}
            <div>
              <strong>Released:</strong>
              {formatReleasedAt(cur.released_at)}
            </div>
          {/if}
        </div>

        {#if cur.notes}
          <div class="notes-section">
            <h3>Release notes</h3>
            <!-- Plain-text rendering: XSS defense, even though notes come
                 from a private DB. Newlines preserved via white-space:
                 pre-wrap. -->
            <pre class="notes-text">{cur.notes}</pre>
          </div>
        {/if}

        {#if error}
          <div class="error-box" role="alert">
            <span>{error}</span>
            <button class="btn-dismiss" onclick={() => dismiss()}>Dismiss</button>
          </div>
        {:else if progress}
          <div class="progress" aria-live="polite">
            <div class="progress-bar">
              <div
                class="progress-fill"
                class:done={progress.state === 'done'}
                style="width: {Math.max(0, Math.min(100, progress.percent))}%"
              ></div>
            </div>
            <div class="progress-message">
              {progress.message} ({Math.round(progress.percent)}%)
            </div>
          </div>
        {:else}
          <div class="actions">
            <button
              class="btn btn-primary"
              disabled={busy}
              title={TIP_NOW}
              onclick={() => choose('now')}
            >
              Fine-tune on my data (recommended)
            </button>
            <button
              class="btn btn-secondary"
              disabled={busy}
              title={TIP_SKIP}
              onclick={() => choose('skip')}
            >
              Use unmodified
            </button>
            <button
              class="btn btn-tertiary"
              disabled={busy}
              title={TIP_LATER}
              onclick={() => choose('later')}
            >
              Skip this update
            </button>
          </div>
        {/if}
      </div>
    {/snippet}
  </DialogRoot>
{/if}

<style>
  .header {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .header h2 {
    flex: 1;
    margin: 0;
    font-size: 17px;
  }
  .badge {
    font-size: 11px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 8px;
    background: var(--color-purple, #8b5cf6);
    color: var(--color-bg, #0f1115);
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 4px 0;
  }
  .meta {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 14px;
    color: var(--color-mid, #9ca3af);
  }
  .meta strong {
    color: var(--color-fg, #f3f4f6);
    font-weight: 600;
  }
  .notes-section h3 {
    margin: 0 0 6px 0;
    font-size: 13px;
    color: var(--color-mid, #9ca3af);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .notes-text {
    margin: 0;
    padding: 10px 12px;
    background: var(--color-bg-elev, rgba(255, 255, 255, 0.04));
    border-radius: 6px;
    font-family: inherit;
    font-size: 13px;
    line-height: 1.45;
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 200px;
    overflow-y: auto;
  }
  .error-box {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    background: rgba(239, 68, 68, 0.12);
    border: 1px solid rgba(239, 68, 68, 0.35);
    border-radius: 6px;
    color: #fca5a5;
    font-size: 13px;
  }
  .btn-dismiss {
    align-self: flex-end;
    padding: 6px 14px;
    border-radius: 6px;
    border: 1px solid rgba(239, 68, 68, 0.4);
    background: transparent;
    color: #fca5a5;
    font-size: 12px;
    cursor: pointer;
  }
  .actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .btn {
    padding: 10px 16px;
    border-radius: 6px;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-primary {
    background: var(--color-purple, #8b5cf6);
    color: var(--color-bg, #0f1115);
    border-color: var(--color-purple, #8b5cf6);
  }
  .btn-secondary {
    background: transparent;
    color: var(--color-fg, #f3f4f6);
    border-color: var(--color-mid, #4b5563);
  }
  .btn-tertiary {
    background: transparent;
    color: var(--color-mid, #9ca3af);
    border-color: transparent;
    font-size: 13px;
  }
  .progress {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .progress-bar {
    height: 6px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 3px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    background: var(--color-teal, #00bfa6);
    transition: width 0.3s ease;
  }
  .progress-fill.done {
    background: var(--color-teal, #00bfa6);
  }
  .progress-message {
    font-size: 12px;
    color: var(--color-mid, #9ca3af);
    text-align: center;
  }
</style>
