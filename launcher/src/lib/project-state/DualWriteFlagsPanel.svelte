<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (c) 2026 VibeCoded Tools -->
<script lang="ts">
  // v0.2.91 WP-L (plan decision #22) — dual embedding / RL logging flags.
  //
  // ONE component, mounted twice with DIFFERENT scopes:
  //
  //   * `<DualWriteFlagsPanel scope="project" projectId={id} />`
  //     — on that project's Settings tab: this project's own choices;
  //   * `<DualWriteFlagsPanel scope="global" />`
  //     — on Preferences: the HOST-WIDE DEFAULTS for projects that have not
  //       chosen.
  //
  // Every string that names a scope comes from `$lib/dual-flags`, derived
  // from this mount's own `scope` prop rather than from which page it happens
  // to sit on. That is what stops two mounts of one component from lying
  // about each other (the decision-#6 rider, as encoded by DeferralLedgerPanel).
  //
  // ## Why the per-project control is a three-way choice, not a checkbox
  //
  // Each flag resolves through three tiers: an explicit per-project row
  // (which wins IN BOTH DIRECTIONS — an explicit off beats a host-wide on),
  // then the host-wide default, then false. So "no row" and "row set to
  // false" are different states, and a checkbox rendered from the EFFECTIVE
  // value cannot tell them apart: a box checked because of a host-wide
  // default, sitting on a per-project page, is indistinguishable from a
  // per-project choice. The third segment ("Use host-wide default") DELETES
  // the row, which is also the only way back to inheriting once a user has
  // clicked anything.
  //
  // ## The dual-log dependency, rendered honestly (F-6)
  //
  // The previous panel DISABLED the dual-log checkbox whenever dual-write was
  // off, while its own tooltip promised "Turning this ON will enable
  // dual-write automatically" — it blocked a supported flow and advertised it
  // in the same element. The backend really does force-enable the
  // prerequisite, so the row is now selectable and a note says what the click
  // will do. The note keys on the RESOLVED dual-write value: under a
  // host-wide `write = true` the log is legitimately reachable with no
  // per-project rows at all.
  //
  // All decision logic lives in `$lib/dual-flags` and is unit-tested there
  // (the repo has no jsdom). This file is markup over it.

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import {
    DUAL_FLAGS,
    TRI_CHOICE_LABELS,
    badgeFor,
    cascadeFootnote,
    effectiveLine,
    globalSaveSummary,
    mountConfigError,
    panelIntro,
    panelTitle,
    rlLogPrerequisiteNote,
    triChoiceFor,
    triChoiceToValue,
    type DualFlagGlobalDefaults,
    type DualFlagKey,
    type DualFlagState,
    type DualFlagsState,
    type DualScope,
    type TriChoice,
  } from '$lib/dual-flags';

  let {
    scope = 'project',
    projectId = undefined,
  }: { scope?: DualScope; projectId?: string } = $props();

  interface GlobalWriteResult {
    defaults: DualFlagGlobalDefaults;
    reprojected: number;
    warnings: number;
    skipped: number;
  }

  let loading = $state(true);
  let loadError = $state<string | null>(null);
  let saving = $state<DualFlagKey | null>(null);
  let flags = $state<DualFlagsState | null>(null);
  let globalDefaults = $state<DualFlagGlobalDefaults | null>(null);

  const TRI_ORDER: TriChoice[] = ['inherit', 'on', 'off'];

  async function load(): Promise<void> {
    // A `scope="project"` mount with no project id can never resolve
    // anything: `load()` would call nothing and the panel would sit on
    // "Loading…" forever. Render the explanation instead.
    const configError = mountConfigError(scope, projectId);
    if (configError) {
      loadError = configError;
      loading = false;
      return;
    }
    loading = true;
    loadError = null;
    try {
      if (scope === 'global') {
        globalDefaults = await invoke<DualFlagGlobalDefaults>(
          'get_dual_flags_global_defaults',
        );
      } else {
        flags = await invoke<DualFlagsState>('get_dual_flags_state', { projectId });
      }
    } catch (e) {
      loadError = String(e);
    } finally {
      loading = false;
    }
  }

  async function chooseProject(key: DualFlagKey, choice: TriChoice): Promise<void> {
    saving = key;
    try {
      // The backend returns the RE-RESOLVED triple, because the coherence
      // cascade may have moved a second flag (turning the log on enables
      // dual-write; turning dual-write off disables the log). Render from
      // that, never from what we optimistically asked for.
      flags = await invoke<DualFlagsState>('set_dual_flag_for_project', {
        projectId,
        flag: key,
        value: triChoiceToValue(choice),
      });
    } catch (e) {
      toast.error(e);
      await load();
    } finally {
      saving = null;
    }
  }

  async function chooseGlobal(key: DualFlagKey, value: boolean): Promise<void> {
    saving = key;
    try {
      const result = await invoke<GlobalWriteResult>('set_dual_flag_global_default', {
        flag: key,
        value,
      });
      globalDefaults = result.defaults;
      toast.success(globalSaveSummary(result.reprojected, result.warnings));
    } catch (e) {
      toast.error(e);
      await load();
    } finally {
      saving = null;
    }
  }

  /**
   * The global mount has no per-project row and no tier above it, so it maps
   * its plain boolean onto the same `DualFlagState` shape the copy helpers
   * take. `explicit` stays null — there is nothing to badge.
   */
  function globalStateFor(key: DualFlagKey): DualFlagState {
    const on = globalDefaults?.[key] ?? false;
    return {
      explicit: null,
      install_default: on,
      effective: on,
      source: on ? 'install_default' : 'system_default',
      clamped: false,
    };
  }

  /**
   * Plain function, not `$derived`: it takes an argument, and Svelte tracks
   * the `$state` reads it performs when the template calls it. A
   * `$derived(fn)` would capture the closure once and lose the dependency on
   * `flags` / `globalDefaults`.
   */
  function stateFor(key: DualFlagKey): DualFlagState {
    if (scope === 'global') return globalStateFor(key);
    return (
      flags?.[key] ?? {
        explicit: null,
        install_default: false,
        effective: false,
        source: 'system_default' as const,
        clamped: false,
      }
    );
  }

  const dualWriteOn = $derived(
    scope === 'global'
      ? (globalDefaults?.write_all_slots ?? false)
      : (flags?.write_all_slots.effective ?? false),
  );

  const ready = $derived(
    !loading && !loadError && (scope === 'global' ? !!globalDefaults : !!flags),
  );

  onMount(load);
  $effect(() => {
    // Re-read when the mount is repointed at a different project.
    if (scope === 'project' && projectId) void load();
  });
</script>

<section class="ps-section df-panel" data-scope={scope}>
  <h2>{panelTitle(scope)}</h2>
  <p class="ps-hint">{panelIntro(scope)}</p>

  {#if loadError}
    <p class="df-error">{loadError}</p>
  {:else if !ready}
    <p class="ps-empty">Loading…</p>
  {:else}
    {#each DUAL_FLAGS as flag (flag.key)}
      {@const st = stateFor(flag.key)}
      {@const badge = badgeFor(scope, st)}
      {@const note =
        flag.key === 'rl_log' ? rlLogPrerequisiteNote(scope, dualWriteOn) : null}
      <div class="df-row" class:df-row-busy={saving === flag.key}>
        <div class="df-text">
          <strong>{flag.label}</strong>
          <small>
            {flag.description}
            Projects as <code>{flag.envVar}</code>.
          </small>
        </div>

        <div class="df-controls">
          {#if scope === 'global'}
            <!-- Two-way at the global tier: there is no tier above it to
                 inherit from, so a third "use the default" option would have
                 nothing to point at. -->
            <div class="df-seg" role="group" aria-label={`${flag.label} host-wide default`}>
              <button
                class="df-seg-btn"
                class:df-seg-active={st.effective}
                aria-pressed={st.effective}
                disabled={saving !== null}
                onclick={() => void chooseGlobal(flag.key, true)}
              >
                On
              </button>
              <button
                class="df-seg-btn"
                class:df-seg-active={!st.effective}
                aria-pressed={!st.effective}
                disabled={saving !== null}
                onclick={() => void chooseGlobal(flag.key, false)}
              >
                Off
              </button>
            </div>
          {:else}
            <div class="df-seg" role="group" aria-label={`${flag.label} for this project`}>
              {#each TRI_ORDER as choice (choice)}
                <button
                  class="df-seg-btn"
                  class:df-seg-active={triChoiceFor(st) === choice}
                  aria-pressed={triChoiceFor(st) === choice}
                  disabled={saving !== null}
                  onclick={() => void chooseProject(flag.key, choice)}
                >
                  {TRI_CHOICE_LABELS[choice]}
                </button>
              {/each}
            </div>
          {/if}

          <p class="df-effective">
            {effectiveLine(scope, st)}
            {#if badge}
              <span class="df-badge" class:df-badge-user={badge.kind === 'user'}
                class:df-badge-auto={badge.kind === 'auto'}>{badge.label}</span>
            {/if}
          </p>
          {#if note}
            <p class="df-note">{note}</p>
          {/if}
        </div>
      </div>
    {/each}

    <footer class="df-footnote">
      <p><strong>How the cascade works:</strong> {cascadeFootnote(scope)[0]}</p>
      {#each cascadeFootnote(scope).slice(1) as line (line)}
        <p>{line}</p>
      {/each}
    </footer>
  {/if}
</section>

<style>
  /* Svelte scopes CSS per component, so the `.ps-section` / `.ps-hint` /
     `.ps-empty` rules in SettingsTab.svelte never reached this file's
     markup — the panel has been rendering unstyled inside a styled tab.
     Now that it also mounts on Preferences (a page with a different section
     idiom entirely), it owns its shell: one set of rules, adjusted per
     `data-scope` so each mount matches its host's headings. */
  .df-panel {
    background: rgba(255, 255, 255, 0.03);
    padding: 14px;
    border-radius: 6px;
    margin-bottom: 14px;
  }
  .df-panel h2 {
    font-size: 13px;
    margin: 0 0 8px;
    color: #c4b3ff;
  }
  /* On Preferences, section headings are small uppercase grey labels. */
  .df-panel[data-scope='global'] {
    background: transparent;
    padding: 0;
    margin-bottom: 0;
  }
  .df-panel[data-scope='global'] h2 {
    font-size: 11px;
    font-weight: 600;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }
  .ps-hint {
    font-size: 11px;
    color: #888;
    margin: 0 0 10px;
    line-height: 1.5;
  }
  .ps-empty {
    padding: 20px;
    text-align: center;
    color: #888;
    font-size: 12px;
  }
  .df-row {
    display: flex;
    gap: 16px;
    align-items: flex-start;
    justify-content: space-between;
    flex-wrap: wrap;
    padding: 10px;
    background: rgba(0, 0, 0, 0.2);
    border-radius: 4px;
    margin-bottom: 8px;
  }
  .df-row-busy {
    opacity: 0.6;
  }
  .df-text {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1 1 320px;
    min-width: 0;
  }
  .df-text strong {
    font-size: 12px;
    color: #f5f5f5;
    font-weight: 600;
  }
  .df-text small {
    font-size: 11px;
    color: #aaa;
    line-height: 1.5;
  }
  .df-text code {
    background: rgba(0, 0, 0, 0.3);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 10px;
  }
  .df-controls {
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: flex-end;
    flex: 0 0 auto;
  }
  .df-seg {
    display: inline-flex;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    overflow: hidden;
  }
  .df-seg-btn {
    background: transparent;
    border: none;
    border-right: 1px solid var(--color-border);
    color: var(--color-mid);
    font-size: 11px;
    padding: 4px 10px;
    cursor: pointer;
  }
  .df-seg-btn:last-child {
    border-right: none;
  }
  .df-seg-btn:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }
  /* The third state is a NAMED, selectable option with its own active
     styling — not the absence of an active button. "Nothing highlighted"
     reads as "nothing selected", which is precisely the ambiguity the
     tri-state exists to remove. */
  .df-seg-active {
    background: rgba(0, 191, 166, 0.18);
    color: var(--color-teal);
    font-weight: 600;
  }
  .df-effective {
    margin: 0;
    font-size: 11px;
    color: var(--color-mid);
    text-align: right;
    max-width: 34ch;
  }
  .df-note {
    margin: 0;
    font-size: 11px;
    color: var(--color-muted);
    text-align: right;
    max-width: 34ch;
  }
  /* Hue convention, copied verbatim from ActiveEmbeddingPicker (the
     semantically closest surface): teal = chosen here, purple = inherited. */
  .df-badge {
    display: inline-block;
    margin-left: 0.4rem;
    padding: 0.05rem 0.4rem;
    border-radius: 0.5rem;
    font-size: 0.72rem;
    vertical-align: middle;
    white-space: nowrap;
  }
  .df-badge-user {
    background: rgba(0, 191, 166, 0.18);
    color: #00bfa6;
  }
  .df-badge-auto {
    background: rgba(123, 95, 255, 0.16);
    color: #7b5fff;
  }
  .df-footnote {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid var(--color-border);
  }
  .df-footnote p {
    margin: 0 0 6px;
    font-size: 11px;
    color: var(--color-muted);
    line-height: 1.5;
  }
  .df-error {
    font-size: 12px;
    color: var(--color-pink);
    line-height: 1.5;
  }
</style>
