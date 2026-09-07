<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // Machine-global Artifact-tool switch, mounted on Preferences.
  //
  // Lives in its own component rather than inline on `preferences/+page.svelte`
  // (already ~4k lines) — the panel-component convention that
  // `GlobalModuleTogglesPanel` and `StorageSettingsCard` already follow.
  //
  // ## What this file is and is not
  //
  // It is markup over `$lib/artifact-tool`, where every decision (checkbox
  // position, when the control is inert, which notice renders) lives and is
  // unit-tested — the repo has no jsdom, so nothing decided inside a `.svelte`
  // file can be covered by a test.
  //
  // It reads the state from the FILE on every load and after every write, via
  // `get_artifact_tool_state`. There is deliberately no launcher.db mirror:
  // the consumer is Claude Code, reading `~/.claude/settings.json`, so a
  // launcher row could only ever be a second answer that disagrees with the
  // one that matters. Nothing is written on load — the file is the user's.

  import { onMount } from 'svelte';
  import { invoke, tauriAvailable } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import {
    CHECKBOX_LABEL,
    RESTART_NOTE,
    REWRITE_NOTE,
    SCOPE_NOTE,
    checkboxChecked,
    controlDisabled,
    notices,
    savedMessage,
    statusLine,
    type ArtifactToolState,
  } from '$lib/artifact-tool';

  let toolState = $state<ArtifactToolState | null>(null);
  let loading = $state(true);
  let saving = $state(false);
  /** Set when the READ itself failed (not when the file merely won't parse —
      that case comes back as a state with `error` and is rendered inline). */
  let loadError = $state<string | null>(null);

  async function load(): Promise<void> {
    if (!tauriAvailable()) {
      loading = false;
      return;
    }
    loading = true;
    loadError = null;
    try {
      toolState = await invoke<ArtifactToolState>('get_artifact_tool_state');
    } catch (e) {
      loadError = String(e);
      toolState = null;
    } finally {
      loading = false;
    }
  }

  async function setEnabled(enabled: boolean): Promise<void> {
    saving = true;
    try {
      // The backend returns the state RE-READ from disk, so what renders is
      // what landed — not what we asked for.
      const next = await invoke<ArtifactToolState>('set_artifact_tool_enabled', {
        enabled,
      });
      toolState = next;
      toast.success(savedMessage(next));
    } catch (e) {
      toast.error(e);
      // A refused write (corrupt file, hostile `permissions` shape) leaves the
      // file untouched; re-read so the panel shows the real reason.
      await load();
    } finally {
      saving = false;
    }
  }

  onMount(load);
</script>

<div class="at-panel">
  <h2>Artifact tool (all projects)</h2>
  <p class="at-hint">
    Claude Code sends the <code>Artifact</code> tool's description in the system
    prompt of every request. If you never create artifacts, turning the tool off
    removes that description and the tokens it costs. VCO writes two keys to
    <code>~/.claude/settings.json</code>: <code>enableArtifact: false</code> and a
    <strong>bare</strong>
    <code>"Artifact"</code> entry in <code>permissions.deny</code> — bare, because
    only the bare name drops the tool from context. A scoped
    <code>Artifact(*)</code> rule would block the call and still pay for the description.
  </p>
  <p class="at-hint at-hint-scope">{SCOPE_NOTE}</p>

  {#if loading}
    <p class="at-empty">Loading…</p>
  {:else if loadError}
    <p class="at-error">Couldn't read the setting: {loadError}</p>
  {:else if !toolState}
    <p class="at-empty">Not available outside the launcher app.</p>
  {:else}
    <div class="at-row" class:at-row-busy={saving}>
      <div class="at-text">
        <strong>{CHECKBOX_LABEL}</strong>
        <small>{statusLine(toolState)}</small>
        <small class="at-path"><code>{toolState.settings_path}</code></small>
      </div>
      <input
        type="checkbox"
        aria-label={CHECKBOX_LABEL}
        checked={checkboxChecked(toolState)}
        disabled={controlDisabled(toolState, saving)}
        onchange={(e) => void setEnabled((e.target as HTMLInputElement).checked)}
      />
    </div>

    {#each notices(toolState) as n (n.kind)}
      <p class="at-notice" class:at-notice-warn={n.tone === 'warn'}>{n.text}</p>
    {/each}

    <p class="at-hint at-hint-foot">{RESTART_NOTE}</p>
    <p class="at-hint">{REWRITE_NOTE}</p>
  {/if}
</div>

<style>
  /* Svelte scopes CSS per component, so the Preferences page's `pr-*` rules
     never reach this markup. The panel owns its shell and matches the
     Preferences section idiom (small uppercase grey heading). */
  .at-panel {
    background: transparent;
    padding: 0;
  }
  .at-panel h2 {
    font-size: 11px;
    font-weight: 600;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    margin: 0 0 8px;
  }
  .at-hint {
    font-size: 11px;
    color: #888;
    margin: 0 0 10px;
    line-height: 1.5;
  }
  .at-hint-scope {
    color: #a99adf;
  }
  .at-hint-foot {
    margin: 10px 0 0;
  }
  .at-empty {
    padding: 16px;
    text-align: center;
    color: #888;
    font-size: 12px;
  }
  .at-error {
    font-size: 11px;
    color: #ff8f8f;
    line-height: 1.5;
    margin: 0;
  }
  .at-row {
    display: flex;
    gap: 16px;
    align-items: flex-start;
    justify-content: space-between;
    flex-wrap: wrap;
    padding: 10px;
    background: rgba(0, 0, 0, 0.2);
    border-radius: 4px;
  }
  .at-row-busy {
    opacity: 0.6;
  }
  .at-text {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1 1 320px;
    min-width: 0;
  }
  .at-text strong {
    font-size: 12px;
    color: #f5f5f5;
    font-weight: 600;
  }
  .at-text small {
    font-size: 11px;
    color: #aaa;
    line-height: 1.5;
  }
  .at-path {
    word-break: break-all;
  }
  .at-notice {
    font-size: 11px;
    color: #aaa;
    line-height: 1.5;
    margin: 8px 0 0;
    padding-left: 10px;
    border-left: 2px solid rgba(255, 255, 255, 0.12);
  }
  .at-notice-warn {
    color: #e6c07b;
    border-left-color: #e6c07b;
  }
  code {
    background: rgba(0, 0, 0, 0.3);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 10px;
  }
</style>
