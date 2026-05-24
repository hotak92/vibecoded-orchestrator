<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // DatePickerControl — native `<input type="date">`.
  //
  // v0.2.32 L5 (2026-05-24).
  //
  // Flow:
  //   1. On mount, load persisted value via `get_module_setting`.
  //      If absent, resolve `control.default` — accepts either an ISO
  //      `YYYY-MM-DD` literal OR the keywords `today` / `30_days_ago`
  //      / `90_days_ago` (resolved against the user's local clock).
  //   2. On change, persist via `set_module_setting` AND fire
  //      `on_change` (if declared) via the shared dispatchAction
  //      helper. The new ISO date string is passed as `value`.
  //   3. "Clear" button writes `null` (RL's `/global/retrain` reads
  //      this as "no earliest_date filter" = all history).
  //
  // The persisted value is also visible to sibling controls via
  // `siblingValuesSnapshot()` in ModuleConfigTab, so a chained_action
  // body can reference it as `{{control:<this_id>}}`.

  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import { dispatchAction } from '$lib/module-dispatch';
  import { toast } from '$lib/stores/toast';
  import type { DatePickerControl } from '$lib/types/manifest';

  let {
    control,
    moduleId,
    projectId,
    disabled = false,
  }: {
    control: DatePickerControl;
    moduleId: string;
    projectId: string;
    disabled?: boolean;
  } = $props();

  // ISO `YYYY-MM-DD` string (HTML date inputs always serialise as
  // ISO; we mirror that on the JS side). Empty string represents
  // "no date selected" — gets persisted as JSON null on submit.
  let value = $state<string>('');
  let busy = $state(false);
  let loading = $state(true);

  const isDisabled = $derived(disabled || busy || loading || projectId === '');

  /**
   * Resolve a keyword default to an ISO date against the user's
   * local wall clock. The renderer does NOT call `Date.UTC()` here
   * — `<input type="date">` uses calendar dates (not timestamps),
   * so we format from the local-clock Date components verbatim.
   *
   * Unrecognised keywords pass through unchanged (treated as ISO
   * literals); the input's native validation surfaces the error
   * if the string isn't a real date.
   */
  function resolveDefault(raw: string | null | undefined): string {
    if (raw === null || raw === undefined || raw === '') return '';
    const now = new Date();
    switch (raw) {
      case 'today':
        return toIsoDate(now);
      case '30_days_ago': {
        const d = new Date(now);
        d.setDate(d.getDate() - 30);
        return toIsoDate(d);
      }
      case '90_days_ago': {
        const d = new Date(now);
        d.setDate(d.getDate() - 90);
        return toIsoDate(d);
      }
      default:
        // Already an ISO literal (or something close enough — the
        // input's native validation catches malformed dates at
        // browser layer).
        return raw;
    }
  }

  function toIsoDate(d: Date): string {
    // YYYY-MM-DD from local-clock components. Pad to 2 digits.
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  onMount(async () => {
    if (!tauriAvailable() || !projectId) {
      // Outside Tauri / no project — still resolve the default so
      // the input shows something sensible in dev mode.
      value = resolveDefault(control.default);
      loading = false;
      return;
    }
    try {
      const persisted = await invoke<unknown>('get_module_setting', {
        moduleId,
        controlId: control.id,
        projectId,
      });
      if (typeof persisted === 'string' && persisted !== '') {
        value = persisted;
      } else if (persisted === null) {
        // Cleared previously — leave value empty (no default
        // resolution; the user explicitly cleared, respect that).
        value = '';
      } else {
        // No persisted row — resolve the default.
        value = resolveDefault(control.default);
      }
    } catch (err) {
      // Soft-fail: missing persisted value is non-fatal. Use the
      // declared default.
      console.warn(
        `[DatePickerControl] get_module_setting failed for ${moduleId}/${control.id}:`,
        err,
      );
      value = resolveDefault(control.default);
    } finally {
      loading = false;
    }
  });

  async function onChange(newValue: string) {
    if (isDisabled) return;
    busy = true;
    value = newValue;
    try {
      // Persist the new value. Empty string maps to JSON `null` so
      // the dispatcher's `{{control:<id>}}` resolver sees `null`
      // (rather than `""`), which sibling buttons can branch on.
      const persistValue: unknown = newValue === '' ? null : newValue;
      await invoke('set_module_setting', {
        moduleId,
        controlId: control.id,
        value: persistValue,
        projectId,
      });
      // Fire on_change if declared. We pass the persisted value
      // (the same one sibling controls would see via the dispatcher's
      // `{{control:<id>}}` resolver).
      if (control.on_change) {
        await dispatchAction({ moduleId, projectId }, control.on_change, persistValue);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error(`${control.label}: ${msg}`);
    } finally {
      busy = false;
    }
  }

  function onClear() {
    void onChange('');
  }

  const inputId = $derived(`date-picker-${control.id}`);
</script>

<div class="date-picker-control">
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
      type="date"
      class="date-input"
      bind:value
      min={control.min ?? undefined}
      max={control.max ?? undefined}
      disabled={isDisabled}
      onchange={(e) => onChange((e.target as HTMLInputElement).value)}
    />
    <button
      type="button"
      class="clear-button"
      onclick={onClear}
      disabled={isDisabled || value === ''}
      aria-label={`Clear ${control.label}`}
      title="Clear date (no filter)"
    >
      Clear
    </button>
  </div>
  {#if loading}
    <p class="loading-msg" aria-live="polite">Loading…</p>
  {/if}
</div>

<style>
  .date-picker-control {
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

  .date-input {
    flex: 1;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.16);
    background: rgba(0, 0, 0, 0.18);
    color: var(--color-text);
    font-size: 13px;
    /* Native date picker hint colours don't always pick up the
       theme; this keeps the visible chrome consistent. */
    color-scheme: dark;
  }
  .date-input:focus {
    outline: none;
    border-color: rgba(0, 191, 166, 0.55);
    box-shadow: 0 0 0 2px rgba(0, 191, 166, 0.20);
  }
  .date-input:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .clear-button {
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.10);
    background: rgba(255, 255, 255, 0.06);
    color: var(--color-text);
    font-size: 12px;
    cursor: pointer;
  }
  .clear-button:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.10);
  }
  .clear-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .loading-msg {
    margin: 0;
    color: var(--color-muted);
    font-size: 12px;
  }
</style>
