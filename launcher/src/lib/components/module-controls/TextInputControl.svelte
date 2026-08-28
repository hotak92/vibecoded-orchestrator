<script lang="ts">
  // TextInputControl — single-line text input + Apply button.
  //
  // v0.2.26 control kind: `text_input`.
  //
  // Flow:
  //   1. On mount, load the persisted value via `get_module_setting`.
  //   2. User edits the input → local state only (no auto-save).
  //   3. User clicks Apply OR presses Enter:
  //       - If `apply_action` is declared, dispatch it with the value.
  //         The response is treated as a validation result:
  //           { valid: bool, message?: string }
  //         The border colour reflects valid/invalid; the message
  //         renders below the input.
  //       - On `valid: true` (or no `apply_action`), persist the value
  //         via `set_module_setting`.
  //   4. Toast on success/error.
  //
  // The validation contract is deliberately loose: any response that
  // doesn't conform to `{ valid, message }` is treated as
  // `valid: true` (the server returned 200 OK, so we assume the value
  // was accepted). This matches what HTTP APIs do today.

  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import { dispatchAction } from '$lib/module-dispatch';
  import { toast } from '$lib/stores/toast';
  import type { TextInputControl } from '$lib/types/manifest';

  type ValidationState = 'unknown' | 'valid' | 'invalid';

  let {
    control,
    moduleId,
    projectId,
    disabled = false,
  }: {
    control: TextInputControl;
    moduleId: string;
    projectId: string;
    disabled?: boolean;
  } = $props();

  // Start empty; `onMount` seeds either from the persisted value (Tauri
  // available + value exists) or from `control.default`. This avoids
  // reading the `control` prop inside a `$state` initializer (which
  // would only capture the initial reference and is flagged by
  // svelte-check as a `state_referenced_locally` mistake).
  let value = $state<string>('');
  let busy = $state(false);
  let loading = $state(true);
  let validation = $state<ValidationState>('unknown');
  let message = $state<string>('');

  const isDisabled = $derived(disabled || busy || loading || projectId === '');

  onMount(async () => {
    // Seed from declared default first; persisted value (if any) wins below.
    value = control.default ?? '';
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
      if (typeof v === 'string') {
        value = v;
      } else if (v !== null && v !== undefined) {
        value = String(v);
      }
    } catch (err) {
      // Soft-fail: missing persisted value is non-fatal. Fall back to default.
      console.warn(
        `[TextInputControl] get_module_setting failed for ${moduleId}/${control.id}:`,
        err,
      );
    } finally {
      loading = false;
    }
  });

  async function apply() {
    if (isDisabled) return;
    busy = true;
    message = '';
    try {
      let isValid = true;
      let validationMessage = '';

      if (control.apply_action) {
        const resp = await dispatchAction<unknown>(
          { moduleId, projectId },
          control.apply_action,
          value,
        );
        // Defensive parsing of the response shape.
        if (resp && typeof resp === 'object') {
          const r = resp as { valid?: unknown; message?: unknown };
          if (typeof r.valid === 'boolean') {
            isValid = r.valid;
          }
          if (typeof r.message === 'string') {
            validationMessage = r.message;
          }
        }
      }

      validation = isValid ? 'valid' : 'invalid';
      message = validationMessage;

      if (isValid) {
        await invoke('set_module_setting', {
          moduleId,
          controlId: control.id,
          value,
          projectId,
        });
        toast.success(`Saved ${control.label}`);
      } else {
        toast.error(validationMessage || `${control.label}: invalid value`);
      }
    } catch (err) {
      validation = 'invalid';
      message = err instanceof Error ? err.message : String(err);
      toast.error(`${control.label}: ${message}`);
    } finally {
      busy = false;
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void apply();
    }
  }

  const inputId = $derived(`text-input-${control.id}`);
</script>

<div class="text-input-control">
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
      type="text"
      class="text-input validation-{validation}"
      bind:value
      placeholder={control.placeholder ?? ''}
      disabled={isDisabled}
      onkeydown={onKeydown}
      aria-invalid={validation === 'invalid'}
      aria-describedby={message ? `${inputId}-msg` : undefined}
    />
    <button
      type="button"
      class="apply-button"
      onclick={apply}
      disabled={isDisabled}
      aria-label={busy ? `Applying ${control.label}` : `Apply ${control.label}`}
    >
      {busy ? '…' : 'Apply'}
    </button>
  </div>
  {#if loading}
    <p class="loading-msg" aria-live="polite">Loading…</p>
  {/if}
  {#if message}
    <p
      id="{inputId}-msg"
      class="validation-message validation-{validation}"
      aria-live="polite"
    >
      {message}
    </p>
  {/if}
</div>

<style>
  .text-input-control {
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

  .text-input {
    flex: 1;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.16);
    background: rgba(0, 0, 0, 0.18);
    color: var(--color-text);
    font-size: 13px;
    transition: border-color 0.12s ease, box-shadow 0.12s ease;
  }
  .text-input:focus {
    outline: none;
    border-color: rgba(0, 191, 166, 0.55);
    box-shadow: 0 0 0 2px rgba(0, 191, 166, 0.20);
  }
  .text-input:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .text-input.validation-valid {
    border-color: rgba(46, 204, 113, 0.55);
  }
  .text-input.validation-invalid {
    border-color: rgba(231, 76, 60, 0.65);
  }

  .apply-button {
    padding: 6px 14px;
    border-radius: 6px;
    border: 1px solid rgba(0, 191, 166, 0.40);
    background: rgba(0, 191, 166, 0.18);
    color: var(--color-teal);
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
  }
  .apply-button:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.28);
  }
  .apply-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .loading-msg {
    margin: 0;
    color: var(--color-muted);
    font-size: 12px;
  }

  .validation-message {
    margin: 0;
    font-size: 12px;
  }
  .validation-message.validation-valid {
    color: #2ecc71;
  }
  .validation-message.validation-invalid {
    color: #e74c3c;
  }
  .validation-message.validation-unknown {
    color: var(--color-muted);
  }
</style>
