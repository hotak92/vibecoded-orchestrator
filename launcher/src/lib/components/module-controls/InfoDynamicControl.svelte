<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // InfoDynamicControl — live read-only info display.
  //
  // v0.2.32 L4 (2026-05-24): bound to a `module_db` source. The
  // renderer fetches the source on mount + on every refresh tick
  // (driven by a section-level `↻` button in ModuleConfigTab.svelte
  // via the `refreshNonce` prop) and surfaces the value through the
  // `format` template's `{value}` token.
  //
  // Unlike StatusDisplayControl (which polls an HTTP endpoint and
  // expects a multi-field response), InfoDynamicControl reads ONE
  // keyed field via the launcher's `module_db_read_row` Tauri command
  // (the Agent-J v0.2.31 path). No container needs to be running.
  // Returns `null` cleanly when the row is absent, the module has no
  // migrations applied, or the hub is unreachable — the component
  // shows the `fallback` text in that case.

  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import type { InfoDynamicControl } from '$lib/types/manifest';

  let {
    control,
    moduleId,
    projectId,
    refreshNonce = 0,
    disabled = false,
  }: {
    control: InfoDynamicControl;
    moduleId: string;
    projectId: string;
    /**
     * Increments when the section-level `↻` button fires. The
     * component re-fetches on every change (via the `$effect` block
     * below). Initial 0 is fine because `onMount` also kicks a fetch.
     */
    refreshNonce?: number;
    disabled?: boolean;
  } = $props();

  // Three rendering states:
  //   - loading: fetch in flight, never resolved.
  //   - resolved: response received (may be null → fallback).
  //   - error: transient error (hub down etc.) — show inline.
  let loading = $state(true);
  let resolved = $state<unknown>(null);
  let error = $state<string>('');
  let lastNonce = -1;

  /**
   * Substitute `{{project_id}}` in the source key against the active
   * project. Other tokens pass through unchanged — manifests may
   * embed them but only `{{project_id}}` is honoured here (the full
   * dispatcher's substitution surface lives behind
   * `module_dispatch_action` and isn't applicable to a direct
   * `module_db_read_row` call).
   */
  function resolveKey(rawKey: string, project: string): string {
    return rawKey.replace(/\{\{\s*project_id\s*\}\}/g, project);
  }

  async function fetchOnce() {
    if (!tauriAvailable()) {
      loading = false;
      return;
    }
    if (!projectId) {
      // No active project → no point hitting the hub. Render fallback.
      loading = false;
      resolved = null;
      return;
    }
    if (control.source.kind !== 'module_db') {
      // Future kinds (`http_endpoint`, `tauri_command`) — surface a
      // clear error so module authors notice when they outpace the
      // launcher version.
      loading = false;
      error = `Unsupported info_dynamic source kind: ${(control.source as { kind?: string }).kind}`;
      return;
    }
    loading = true;
    error = '';
    try {
      const key = resolveKey(control.source.key, projectId);
      const body = await invoke<Record<string, unknown> | null>('module_db_read_row', {
        moduleId,
        projectId,
        table: control.source.table,
        key,
        // Projection: only request the field we need (saves bandwidth
        // when the row is wide).
        fields: [control.source.field],
      });
      if (body === null || body === undefined) {
        resolved = null;
      } else {
        resolved = (body as Record<string, unknown>)[control.source.field] ?? null;
      }
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      // Don't toast — the section may have many *_dynamic controls
      // and toasting on every read failure (hub restart, container
      // not running) would spam. Inline error suffices.
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void fetchOnce();
  });

  // Re-fetch whenever the section-level refresh nonce ticks. We
  // gate on `lastNonce` to avoid double-firing on the initial mount
  // (onMount already kicks one fetch).
  $effect(() => {
    if (refreshNonce !== lastNonce && refreshNonce > 0) {
      lastNonce = refreshNonce;
      void fetchOnce();
    }
  });

  // Stringify a scalar JSON value for `{value}` substitution. Strings
  // pass through; numbers / booleans coerce via `String()`; arrays /
  // objects fall back to JSON.stringify (defensive — the canonical
  // use case is scalar fields like `weights_version`).
  function valueText(v: unknown): string {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    try {
      return JSON.stringify(v);
    } catch {
      return '';
    }
  }

  const displayText = $derived.by(() => {
    if (loading) return 'Loading…';
    if (error) return error;
    if (resolved === null || resolved === undefined) {
      return control.fallback ?? '';
    }
    const fmt = control.format ?? '{value}';
    return fmt.replace(/\{value\}/g, valueText(resolved));
  });
</script>

<div class="info-dynamic-control" class:disabled>
  <div class="control-label-row">
    <span class="control-label">{control.label}</span>
    <span
      class="tooltip-affordance"
      title={control.tooltip ?? control.label}
      aria-label="More info"
    >?</span>
  </div>
  <div
    class="value-card"
    class:loading
    class:error={error !== ''}
    role="status"
    aria-live="polite"
  >
    <span class="value-text">{displayText}</span>
  </div>
</div>

<style>
  .info-dynamic-control {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .info-dynamic-control.disabled {
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

  .value-card {
    padding: 8px 12px;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    font-size: 13px;
    font-family: var(--font-mono, ui-monospace, 'SF Mono', Menlo, monospace);
    color: var(--color-text);
    min-height: 1.6em;
  }
  .value-card.loading {
    color: var(--color-muted);
    font-style: italic;
  }
  .value-card.error {
    background: rgba(231, 76, 60, 0.08);
    border-color: rgba(231, 76, 60, 0.30);
    color: #e74c3c;
  }

  .value-text {
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
