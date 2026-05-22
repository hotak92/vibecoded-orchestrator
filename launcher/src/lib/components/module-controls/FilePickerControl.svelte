<script lang="ts">
  // FilePickerControl — native file or directory picker.
  //
  // v0.2.26 control kind: `file_picker`.
  //
  // Flow:
  //   1. On mount, load the persisted path via `get_module_setting`.
  //   2. User clicks "Choose…" → opens the native Tauri dialog
  //      (`@tauri-apps/plugin-dialog`).
  //        - If `directory: true`, picks a folder.
  //        - Otherwise picks a single file, filtered by `extensions`.
  //   3. On selection, persist via `set_module_setting` and dispatch
  //      `on_change` if declared.
  //   4. "Clear" button wipes the persisted value back to empty string.
  //
  // The native dialog is provided by `@tauri-apps/plugin-dialog`; in
  // browser mode (vite preview) `pickFile` / `pickDirectory` return null
  // and the button no-ops.

  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import { pickFile, pickDirectory } from '$lib/dialog';
  import { dispatchAction } from '$lib/module-dispatch';
  import { toast } from '$lib/stores/toast';
  import type { FilePickerControl } from '$lib/types/manifest';

  let {
    control,
    moduleId,
    projectId,
    disabled = false,
  }: {
    control: FilePickerControl;
    moduleId: string;
    projectId: string;
    disabled?: boolean;
  } = $props();

  let path = $state<string>('');
  let busy = $state(false);
  let loading = $state(true);
  let error = $state<string>('');

  const isDirectoryMode = $derived(control.directory === true);
  const isDisabled = $derived(disabled || busy || loading || projectId === '');

  onMount(async () => {
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
        path = v;
      }
    } catch (err) {
      console.warn(
        `[FilePickerControl] get_module_setting failed for ${moduleId}/${control.id}:`,
        err,
      );
    } finally {
      loading = false;
    }
  });

  async function choose() {
    if (isDisabled) return;
    busy = true;
    error = '';
    try {
      let picked: string | null;
      if (isDirectoryMode) {
        picked = await pickDirectory({
          title: `Select directory for ${control.label}`,
          defaultPath: path || undefined,
        });
      } else {
        const extensions = control.extensions ?? [];
        picked = await pickFile({
          title: `Select file for ${control.label}`,
          defaultPath: path || undefined,
          filters:
            extensions.length > 0
              ? [{ name: 'Allowed', extensions: [...extensions] }]
              : undefined,
        });
      }
      if (picked === null) {
        // User cancelled — no-op.
        return;
      }
      await persist(picked);
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      toast.error(`${control.label}: ${error}`);
    } finally {
      busy = false;
    }
  }

  async function clear() {
    if (isDisabled) return;
    busy = true;
    error = '';
    try {
      await persist('');
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      toast.error(`${control.label}: ${error}`);
    } finally {
      busy = false;
    }
  }

  async function persist(value: string) {
    path = value;
    await invoke('set_module_setting', {
      moduleId,
      controlId: control.id,
      value,
      projectId,
    });
    if (control.on_change) {
      await dispatchAction({ moduleId, projectId }, control.on_change, value);
    }
    toast.success(
      value === ''
        ? `Cleared ${control.label}`
        : `Saved ${control.label}`,
    );
  }

  const displayPath = $derived(path === '' ? '(none selected)' : path);
  const buttonLabel = $derived(
    isDirectoryMode ? 'Choose folder…' : 'Choose file…',
  );
  const inputId = $derived(`file-picker-${control.id}`);
</script>

<div class="file-picker-control">
  <div class="control-label-row">
    <span class="control-label" id="{inputId}-label">{control.label}</span>
    <span
      class="tooltip-affordance"
      title={control.tooltip ?? control.label}
      aria-label="More info"
    >?</span>
  </div>
  <div class="picker-row">
    <span
      class="path-display"
      class:empty={path === ''}
      title={path || ''}
      aria-labelledby="{inputId}-label"
    >
      {displayPath}
    </span>
    <button
      type="button"
      class="picker-button"
      onclick={choose}
      disabled={isDisabled}
      aria-label={`${buttonLabel} for ${control.label}`}
    >
      {busy ? '…' : buttonLabel}
    </button>
    {#if path !== ''}
      <button
        type="button"
        class="clear-button"
        onclick={clear}
        disabled={isDisabled}
        aria-label={`Clear ${control.label}`}
      >
        Clear
      </button>
    {/if}
  </div>
  {#if loading}
    <p class="loading-msg" aria-live="polite">Loading…</p>
  {/if}
  {#if !isDirectoryMode && control.extensions && control.extensions.length > 0}
    <p class="hint">
      Allowed: {control.extensions.map((e) => `.${e}`).join(', ')}
    </p>
  {/if}
  {#if error}
    <p class="error-message" aria-live="polite">{error}</p>
  {/if}
</div>

<style>
  .file-picker-control {
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

  .picker-row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .path-display {
    flex: 1;
    min-width: 0;
    padding: 6px 10px;
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.10);
    background: rgba(0, 0, 0, 0.18);
    font-family: var(--font-mono, ui-monospace, 'SF Mono', Menlo, monospace);
    font-size: 12px;
    color: var(--color-text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    direction: ltr;
  }
  .path-display.empty {
    color: var(--color-muted);
    font-style: italic;
  }

  .picker-button {
    padding: 6px 12px;
    border-radius: 6px;
    border: 1px solid rgba(0, 191, 166, 0.40);
    background: rgba(0, 191, 166, 0.18);
    color: #00bfa6;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }
  .picker-button:hover:not(:disabled) {
    background: rgba(0, 191, 166, 0.28);
  }
  .picker-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .clear-button {
    padding: 6px 10px;
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

  .hint {
    margin: 0;
    color: var(--color-muted);
    font-size: 11px;
  }

  .error-message {
    margin: 0;
    color: #e74c3c;
    font-size: 12px;
  }
</style>
