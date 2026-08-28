<script lang="ts">
  // NumberInputControl — numeric input with min/max/step.
  //
  // v0.2.26 control kind: `number_input`.
  //
  // Flow:
  //   1. On mount, load the persisted value via `get_module_setting`.
  //   2. User edits → on blur (or Enter), clamp to [min,max], persist
  //      via `set_module_setting`, and dispatch `on_change` if declared.
  //   3. Toast on success/error.
  //
  // The JSON wire type is `number` — we persist the parsed float, NOT
  // the raw string from the input.
  //
  // v0.2.91 (P2-M6): unparseable input is REJECTED, not coerced. It used
  // to fall back to the control's declared default and run through the
  // same persist-and-toast path as a legitimate empty-field clear, so the
  // user was told "Saved <label>" while a value they never typed was
  // written. The decision now lives in `./numberInputCommit` so the
  // clear-vs-garbage branch is unit-testable; an empty field still means
  // "restore the default".

  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import { dispatchAction } from '$lib/module-dispatch';
  import { toast } from '$lib/stores/toast';
  import { decideNumberCommit } from './numberInputCommit';
  import type { NumberInputControl } from '$lib/types/manifest';

  let {
    control,
    moduleId,
    projectId,
    disabled = false,
  }: {
    control: NumberInputControl;
    moduleId: string;
    projectId: string;
    disabled?: boolean;
  } = $props();

  // Raw input value (kept as string so the user can type intermediate
  // states like "1." or "-" without us clobbering their typing). Parsed
  // to a number on blur / Enter / commit.
  //
  // Seeded by `onMount` from either the persisted value or
  // `control.default` — see comment in TextInputControl for why we
  // don't read the prop inside the `$state` initializer.
  let rawValue = $state<string>('');
  let busy = $state(false);
  let loading = $state(true);
  let error = $state<string>('');

  const isDisabled = $derived(disabled || busy || loading || projectId === '');

  onMount(async () => {
    // Seed from declared default; persisted value (if any) wins below.
    if (control.default !== null && control.default !== undefined) {
      rawValue = String(control.default);
    }
    if (!tauriAvailable() || !projectId) {
      loading = false;
      return;
    }
    try {
      const v = await invoke<unknown>('get_module_setting', {
        moduleId,
        controlId: control.id,
        projectId,
      });
      if (typeof v === 'number' && Number.isFinite(v)) {
        rawValue = String(v);
      } else if (typeof v === 'string' && v !== '') {
        rawValue = v;
      }
    } catch (err) {
      console.warn(
        `[NumberInputControl] get_module_setting failed for ${moduleId}/${control.id}:`,
        err,
      );
    } finally {
      loading = false;
    }
  });

  /**
   * Commit on blur / Enter.
   *
   * `badInput` comes from the native input's ValidityState: a
   * `<input type="number">` reports unparseable text ("abc") as an EMPTY
   * value with that flag set, so without it garbage is indistinguishable
   * from a deliberate clear — which is exactly how the silent
   * default-substitution went unnoticed.
   */
  async function commit(badInput = false) {
    if (isDisabled) return;
    const decision = decideNumberCommit(rawValue, control, badInput);
    if (decision.action === 'reject') {
      // Inline error, no persist, and no "Saved" toast. The user's text
      // stays in the field so they can correct it.
      error = decision.message;
      return;
    }
    error = '';
    // Reflect the clamped / defaulted value back into the input.
    if (decision.display !== rawValue) {
      rawValue = decision.display;
    }
    await commitValue(decision.value);
  }

  function onBlur(e: FocusEvent & { currentTarget: HTMLInputElement }) {
    void commit(e.currentTarget.validity?.badInput ?? false);
  }

  async function commitValue(n: number) {
    busy = true;
    error = '';
    try {
      await invoke('set_module_setting', {
        moduleId,
        controlId: control.id,
        value: n,
        projectId,
      });
      if (control.on_change) {
        await dispatchAction({ moduleId, projectId }, control.on_change, n);
      }
      toast.success(`Saved ${control.label}`);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      toast.error(`${control.label}: ${error}`);
    } finally {
      busy = false;
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      const input = e.target as HTMLInputElement;
      void commit(input.validity?.badInput ?? false);
      input.blur();
    }
  }

  const inputId = $derived(`number-input-${control.id}`);
</script>

<div class="number-input-control">
  <div class="control-label-row">
    <label class="control-label" for={inputId}>{control.label}</label>
    <span
      class="tooltip-affordance"
      title={control.tooltip ?? control.label}
      aria-label="More info"
    >?</span>
  </div>
  <div class="input-row">
    <input
      id={inputId}
      type="number"
      class="number-input"
      bind:value={rawValue}
      min={control.min ?? undefined}
      max={control.max ?? undefined}
      step={control.step ?? undefined}
      disabled={isDisabled}
      onblur={onBlur}
      onkeydown={onKeydown}
      aria-invalid={error !== ''}
      aria-describedby={error ? `${inputId}-err` : undefined}
    />
    {#if busy}
      <span class="busy-spinner" aria-label="Saving">…</span>
    {/if}
  </div>
  {#if loading}
    <p class="loading-msg" aria-live="polite">Loading…</p>
  {/if}
  {#if error}
    <p id="{inputId}-err" class="error-message" aria-live="polite">{error}</p>
  {/if}
</div>

<style>
  .number-input-control {
    display: flex;
    flex-direction: column;
    gap: 6px;
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

  .input-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .number-input {
    width: 140px;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.16);
    background: rgba(0, 0, 0, 0.18);
    color: var(--color-text);
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }
  .number-input:focus {
    outline: none;
    border-color: rgba(0, 191, 166, 0.55);
    box-shadow: 0 0 0 2px rgba(0, 191, 166, 0.20);
  }
  .number-input:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .busy-spinner {
    font-size: 14px;
    color: var(--color-muted);
  }

  .loading-msg {
    margin: 0;
    color: var(--color-muted);
    font-size: 12px;
  }

  .error-message {
    margin: 0;
    color: #e74c3c;
    font-size: 12px;
  }
</style>
