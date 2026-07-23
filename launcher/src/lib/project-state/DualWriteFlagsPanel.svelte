<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<script lang="ts">
  // v0.2.71 T-B-flags — per-project dual-write + dual-log toggles.
  //
  // Two boolean flags, default OFF, with launcher.db `module_settings` as
  // the single source of truth. The hub resolver + config_projection stamp
  // them into .claude/{settings.json,env} as DUAL_EMBEDDING_WRITE_ALL_SLOTS
  // and DUAL_RL_LOG_ENABLED. Mirrors the well-behaved
  // `module_set_enabled_for_project` reference: GUI writes the DB, the
  // env projection follows.
  //
  //   * dual_embedding_write_all_slots (orchestrator-core) — writes
  //     embeddings to ALL named-vector slots (e.g. the secondary openai
  //     slot alongside the primary qwen3 slot). Costs extra embed calls;
  //     opt-in.
  //   * dual_rl_log_enabled (vct-rl-reranker) — also logs RL events under
  //     the secondary slot. DEPENDS on dual-write: you can't log into a
  //     secondary slot that isn't being populated. The dual-log checkbox is
  //     therefore disabled until dual-write is ON, and turning dual-log ON
  //     force-enables dual-write (the backend setter cascades the same way,
  //     so the DB can never reach the incoherent log=true/write=false pair).
  //   * dual_embedding_arctic_secondary (orchestrator-core) — v0.2.88
  //     (DEFECT 5): also writes into a SECONDARY arctic slot. INDEPENDENT of
  //     the two above (no cascade). Added so all three dual-write flags share
  //     ONE canonical channel (DB → projection → env). Projects as
  //     DUAL_EMBEDDING_ARCTIC_SECONDARY.
  //
  // Both choices survive bundle/orchestrator updates: they live in
  // module_settings, which no update path resets (only bundled files get
  // re-synced).

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';

  let { projectId }: { projectId: string } = $props();

  let loading = $state(true);
  let savingWrite = $state(false);
  let savingLog = $state(false);
  let savingArctic = $state(false);
  let dualWrite = $state(false);
  let dualLog = $state(false);
  let arcticSecondary = $state(false);

  async function load(): Promise<void> {
    loading = true;
    try {
      dualWrite = await invoke<boolean>('get_dual_embedding_write_all_slots', {
        projectId,
      });
      dualLog = await invoke<boolean>('get_dual_rl_log_enabled', { projectId });
      arcticSecondary = await invoke<boolean>(
        'get_dual_embedding_arctic_secondary',
        { projectId },
      );
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  // Re-project .claude/{settings.json,env} so the DUAL_* env vars reflect
  // the new DB state immediately (no session restart needed). The DB write
  // already landed, so a projection hiccup is a warning, not a failure.
  async function reproject(): Promise<void> {
    try {
      await invoke('refresh_project_env', { projectId });
    } catch (e) {
      toast.error(
        `Saved the flag, but re-projecting env files failed: ${e}`,
      );
    }
  }

  async function toggleWrite(next: boolean): Promise<void> {
    savingWrite = true;
    try {
      await invoke('set_dual_embedding_write_all_slots', {
        projectId,
        value: next,
      });
      await reproject();
      // Reload to pick up the cascade: disabling dual-write also disables
      // dual-log backend-side, so the dual-log checkbox must re-read.
      await load();
    } catch (e) {
      toast.error(e);
      await load();
    } finally {
      savingWrite = false;
    }
  }

  async function toggleLog(next: boolean): Promise<void> {
    savingLog = true;
    try {
      await invoke('set_dual_rl_log_enabled', { projectId, value: next });
      await reproject();
      // Reload to pick up the cascade: enabling dual-log force-enables
      // dual-write backend-side, so the dual-write checkbox must re-read.
      await load();
    } catch (e) {
      toast.error(e);
      await load();
    } finally {
      savingLog = false;
    }
  }

  // v0.2.88 (DEFECT 5): independent toggle — no cascade to/from the other two.
  async function toggleArctic(next: boolean): Promise<void> {
    savingArctic = true;
    try {
      await invoke('set_dual_embedding_arctic_secondary', {
        projectId,
        value: next,
      });
      await reproject();
      arcticSecondary = next;
    } catch (e) {
      toast.error(e);
      await load();
    } finally {
      savingArctic = false;
    }
  }

  onMount(load);
  $effect(() => {
    if (projectId) void load();
  });
</script>

<section class="ps-section">
  <h2>Dual embedding / RL logging (this project)</h2>
  <p class="ps-hint">
    Opt-in per-project toggles, stored in the launcher database (not a
    bundled file) — they <strong>survive bundle / orchestrator updates</strong>.
    Both default OFF.
  </p>

  {#if loading}
    <p class="ps-empty">Loading…</p>
  {:else}
    <label class="ps-flag-check">
      <input
        type="checkbox"
        checked={dualWrite}
        disabled={savingWrite}
        onchange={(e) => toggleWrite((e.target as HTMLInputElement).checked)}
      />
      <span>
        <strong>Write embeddings to all named-vector slots</strong>
        <small>
          Projects as <code>DUAL_EMBEDDING_WRITE_ALL_SLOTS</code>. When ON,
          the indexer writes to every configured embedding slot (e.g. a
          secondary <code>openai</code> slot alongside the primary
          <code>qwen3</code> slot). Costs extra embed calls per node — leave
          OFF unless you need multi-slot search.
        </small>
      </span>
    </label>

    <label
      class="ps-flag-check"
      class:ps-flag-disabled={!dualWrite}
      title={!dualWrite
        ? 'Requires "Write embeddings to all named-vector slots" — RL events can only be logged into a secondary slot that is being populated. Turning this ON will enable dual-write automatically.'
        : 'Also log RL events under the secondary embedding slot'}
    >
      <input
        type="checkbox"
        checked={dualLog}
        disabled={savingLog || (!dualWrite && !dualLog)}
        onchange={(e) => toggleLog((e.target as HTMLInputElement).checked)}
      />
      <span>
        <strong>Log RL events under the secondary slot</strong>
        <small>
          Projects as <code>DUAL_RL_LOG_ENABLED</code>. Depends on dual-write
          above (you can't log into a slot that isn't populated). Enabling
          this auto-enables dual-write; disabling dual-write auto-disables
          this.
        </small>
      </span>
    </label>

    <!-- v0.2.88 (DEFECT 5): independent arctic-secondary toggle. -->
    <label class="ps-flag-check">
      <input
        type="checkbox"
        checked={arcticSecondary}
        disabled={savingArctic}
        onchange={(e) => toggleArctic((e.target as HTMLInputElement).checked)}
      />
      <span>
        <strong>Write a secondary arctic embedding slot</strong>
        <small>
          Projects as <code>DUAL_EMBEDDING_ARCTIC_SECONDARY</code>. When ON, the
          indexer also writes an <code>arctic</code> secondary slot alongside
          the active slot (e.g. a qwen3-active install can collect an arctic
          corpus for later reranking). Independent of the two above — no
          dependency either way. Costs extra embed calls; opt-in.
        </small>
      </span>
    </label>
  {/if}
</section>

<style>
  .ps-flag-check {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 8px 10px;
    background: rgba(0, 0, 0, 0.2);
    border-radius: 4px;
    cursor: pointer;
    user-select: none;
    margin-bottom: 8px;
  }
  .ps-flag-check input[type='checkbox'] {
    margin-top: 2px;
    flex-shrink: 0;
  }
  .ps-flag-check span {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1;
  }
  .ps-flag-check strong {
    font-size: 12px;
    color: #f5f5f5;
    font-weight: 600;
  }
  .ps-flag-check small {
    font-size: 11px;
    color: #aaa;
    line-height: 1.5;
  }
  .ps-flag-check code {
    background: rgba(0, 0, 0, 0.3);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 10px;
  }
  /* Greyed dependent row: the dual-log checkbox is unreachable until
     dual-write is ON. Cursor + opacity signal the gate; the tooltip on the
     label explains why. */
  .ps-flag-disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .ps-flag-disabled input[type='checkbox'] {
    cursor: not-allowed;
  }
</style>
