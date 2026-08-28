<!--
  v0.2.52 V52-AD — Global (host-wide) module enable/disable panel.

  Mount site: /preferences/modules route. Sibling to per-project Modules
  panels that surface the per-project toggle. The two surfaces share the
  same underlying `module_settings` table (via migration 034's nullable
  project_id) and the same Tauri event channel
  `module:enabled-for-project-changed`.

  Scope:

    * Initial release targets `vct-rl-reranker` only — the user-stated
      immediate need is "disable RL until 500+ retrieval events
      accumulate". The component lists a single row for now; future
      modules can be added to KNOWN_GLOBAL_MODULES below.

    * Reader cascade (per-project → global → fail-open true) is computed
      backend-side. This panel edits the GLOBAL tier only and says so;
      the per-project tier is edited from each module's tile on the
      project's Modules page (v0.2.91 decision #23 — before that the
      per-project control did not exist, which is why the pointer to it
      was removed in F-2 and is restored here now that it does).

  v0.2.91 decision #23 (F-4) — the third position:

    `module_set_global_enabled` is DELETE+INSERT and can never leave the
    row ABSENT, and the only method that removed it was uninstall-time
    and unexposed. So `null` — "no host-wide choice set", the state EVERY
    module is in on a fresh install — became unreachable the moment a
    user clicked either button, while `describeGlobal` still had copy for
    it. A named third button now clears the row
    (`module_clear_global_enabled`), and `null` is rendered as a chosen
    position rather than as "neither button highlighted".

  Architecture choice (NOT a dynamic catalog walk):
    The catalog of which modules are "global-scope" lives backend-side
    in each module's vct-module.json manifest (`install.scope == "global"`).
    The launcher has a backend helper that enumerates them, but exposing
    it requires a manifest-walk Tauri command that doesn't exist today.
    For V52-AD's narrow scope, the hardcoded list is sufficient — extending
    to a dynamic list is queued for V52-AD follow-up.

  Soft-fail design:
    Every Tauri call is wrapped in try/catch; failed reads render
    "status unavailable" rather than blanking the row. Mirrors
    RlRerankerStatusPanel.svelte's pattern.
-->

<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { invoke, tauriAvailable, listen as tauriListen } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  // v0.2.91 decision #23: shared copy + position logic, so this panel and
  // the per-project tile control cannot word the same cascade differently.
  import {
    GLOBAL_TRI_CHOICE_LABELS,
    dormantNotice,
    globalDefaultLine,
    globalTriChoiceFor,
    globalTriChoiceToValue,
    type GlobalTriChoice,
  } from '$lib/module-enable';

  // Hardcoded list of global-scope modules whose toggle appears in this
  // panel. Add new module_ids here as more `install.scope = "global"`
  // modules ship. Keep in sync with backend manifest registry.
  const KNOWN_GLOBAL_MODULES: Array<{
    id: string;
    label: string;
    description: string;
    /** Suggested threshold for an "auto-enable" prompt; null = none. */
    autoEnableEventThreshold?: number;
  }> = [
    {
      id: 'vct-rl-reranker',
      label: 'RL Reranker',
      description:
        'Reinforcement-learning-based reranking of KG retrieval results. ' +
        'Disabled by default on fresh installs until enough training data ' +
        'accumulates. Per-project overrides take precedence.',
      autoEnableEventThreshold: 500,
    },
  ];

  type ModuleState = {
    /** null = no global row written (system default true applies). */
    globalEnabled: boolean | null;
    /** Current rl_events row count (only meaningful for vct-rl-reranker). */
    rlEventsCount: number | null;
    /** True while a write or read is in flight. */
    pending: boolean;
    /** Last error message; clears on successful read. */
    error: string | null;
  };

  let states = $state<Record<string, ModuleState>>({});
  let initializing = $state(true);
  let unlistenChange: (() => void) | undefined;

  function newState(): ModuleState {
    return {
      globalEnabled: null,
      rlEventsCount: null,
      pending: false,
      error: null,
    };
  }

  async function loadOne(moduleId: string) {
    if (!tauriAvailable()) {
      states[moduleId] = newState();
      return;
    }
    const s = states[moduleId] ?? newState();
    s.pending = true;
    s.error = null;
    states[moduleId] = s;
    try {
      const [globalEnabled, rlCount] = await Promise.all([
        invoke<boolean | null>('module_is_global_enabled', { moduleId }),
        // rl_events_count is currently global (not per-module). Surface
        // the same count for every module; future per-module event
        // counters can refine this.
        invoke<number>('rl_events_count').catch(() => null),
      ]);
      s.globalEnabled = globalEnabled;
      s.rlEventsCount = rlCount;
    } catch (e) {
      s.error = e instanceof Error ? e.message : String(e);
      console.warn(`[GlobalModuleTogglesPanel] load(${moduleId}) failed:`, s.error);
    } finally {
      s.pending = false;
      states[moduleId] = s;
    }
  }

  async function loadAll() {
    initializing = true;
    for (const m of KNOWN_GLOBAL_MODULES) {
      states[m.id] = newState();
    }
    await Promise.all(KNOWN_GLOBAL_MODULES.map((m) => loadOne(m.id)));
    initializing = false;
  }

  /**
   * Write — or CLEAR — the host-wide default.
   *
   * `choice === 'default'` clears the row so the module falls back to the
   * system default. That is the way back out of a state the user could
   * previously enter but never leave (F-4); it is a first-class position,
   * not a reset button hidden elsewhere.
   */
  async function setGlobal(moduleId: string, choice: GlobalTriChoice) {
    if (!tauriAvailable()) return;
    const s = states[moduleId] ?? newState();
    s.pending = true;
    states[moduleId] = s;
    const value = globalTriChoiceToValue(choice);
    try {
      if (value === null) {
        // Returns the post-clear value (always null) — render from what the
        // backend reports, not from what we asked for.
        s.globalEnabled = await invoke<boolean | null>('module_clear_global_enabled', {
          moduleId,
        });
        toast.success(
          `Host-wide choice for ${displayLabel(moduleId)} cleared — projects ` +
            'that have not chosen now follow the system default.',
        );
      } else {
        await invoke('module_set_global_enabled', { moduleId, enabled: value });
        s.globalEnabled = value;
        toast.success(
          `Host-wide default for ${displayLabel(moduleId)} set to ${
            value ? 'ON' : 'OFF'
          }`,
        );
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(`Failed to change host-wide default: ${msg}`);
      s.error = msg;
    } finally {
      s.pending = false;
      states[moduleId] = s;
    }
  }

  function displayLabel(moduleId: string): string {
    return KNOWN_GLOBAL_MODULES.find((m) => m.id === moduleId)?.label ?? moduleId;
  }

  /**
   * Render text describing the current host-wide default. The `null` branch
   * is a REAL state (no row written), not a loading placeholder — loading
   * and read-failure are separate branches above it. Wording comes from
   * `$lib/module-enable` so this panel and the per-project tile agree.
   */
  function describeGlobal(s: ModuleState): string {
    if (s.pending) return 'loading…';
    if (s.error) return 'status unavailable';
    return globalDefaultLine(s.globalEnabled);
  }

  /**
   * Render the auto-enable progress indicator for modules with a
   * training-data threshold. Returns null if not applicable.
   */
  function autoEnableProgress(moduleId: string, s: ModuleState): string | null {
    const m = KNOWN_GLOBAL_MODULES.find((x) => x.id === moduleId);
    if (!m?.autoEnableEventThreshold) return null;
    if (s.rlEventsCount === null) return null;
    const threshold = m.autoEnableEventThreshold;
    const pct = Math.min(100, Math.round((s.rlEventsCount / threshold) * 100));
    if (s.rlEventsCount >= threshold) {
      // `rl_events_count` is SELECT COUNT(*) FROM rl_events — ALL event types
      // (retrieval + citation + bash_outcome/edit_outcome + pre_bash), not
      // retrieval alone. Label accordingly (v0.2.70 A2-NIT2).
      return `${s.rlEventsCount.toLocaleString()} RL events accumulated — threshold met`;
    }
    return `${s.rlEventsCount.toLocaleString()} / ${threshold.toLocaleString()} RL events (${pct}%)`;
  }

  onMount(() => {
    void loadAll();
    if (tauriAvailable()) {
      tauriListen<{ project_id: string; module_id: string; enabled: boolean }>(
        'module:enabled-for-project-changed',
        (e) => {
          // Reload affected row on any change. Both per-project and
          // global flips fire this event; we reload either way since
          // the global-default display doesn't depend on the trigger
          // type.
          const moduleId = e.payload.module_id;
          if (KNOWN_GLOBAL_MODULES.some((m) => m.id === moduleId)) {
            void loadOne(moduleId);
          }
        },
      ).then((un) => {
        unlistenChange = un;
      });
    }
  });

  onDestroy(() => {
    unlistenChange?.();
  });
</script>

<div class="gmp">
  <header>
    <h2>Module defaults (host-wide)</h2>
    <p class="hint">
      <!-- v0.2.91: F-2 removed a clause pointing at a per-project control
           that did not exist — `module_settings.enabled_for_project` had
           ZERO GUI writers, while the tile's "Enabled" checkbox wrote a
           DIFFERENT table (`module_installs.enabled`). Decision #23 BUILT
           that control, so the pointer is restored — now naming the surface
           that really writes this key. If the tile control is ever removed
           again, remove this clause with it. -->
      These settings control whether modules are <em>on by default</em> for
      every project on this machine. A project that has made its own choice —
      on each module's tile in that project's Modules page — keeps it,
      including an explicit off while the default here is on.
    </p>
  </header>

  {#if initializing}
    <div class="loading">Loading module defaults…</div>
  {:else}
    <ul class="rows">
      {#each KNOWN_GLOBAL_MODULES as m (m.id)}
        {@const s = states[m.id] ?? newState()}
        <li class="row" class:disabled={s.pending}>
          <div class="row-main">
            <div class="row-text">
              <div class="row-label">{m.label}</div>
              <div class="row-desc">{m.description}</div>
              <div class="row-status">
                Current global default: <strong>{describeGlobal(s)}</strong>
              </div>
              {#if autoEnableProgress(m.id, s)}
                <div class="row-progress">
                  {autoEnableProgress(m.id, s)}
                </div>
              {/if}
            </div>
            <div class="row-controls" role="group" aria-label={`${m.label} host-wide default`}>
              <!-- v0.2.91 F-4: THREE positions. "Use system default" clears
                   the row; without it the initial `null` state (which every
                   module starts in) is unreachable after the first click,
                   and while in it NEITHER button carried `.active`, so a
                   real named state rendered as "nothing selected". -->
              {#each ['default', 'on', 'off'] as const as choice (choice)}
                <button
                  class="toggle"
                  class:active={globalTriChoiceFor(s.globalEnabled) === choice}
                  aria-pressed={globalTriChoiceFor(s.globalEnabled) === choice}
                  disabled={s.pending}
                  onclick={() => setGlobal(m.id, choice)}
                >
                  {GLOBAL_TRI_CHOICE_LABELS[choice]}
                </button>
              {/each}
            </div>
          </div>
          {#if dormantNotice(m.id)}
            <!-- USER rider (#23): the switch is real, its EFFECT is pending.
                 Say so, so "Enabled" never reads as "actively reranking". -->
            <div class="row-dormant">{dormantNotice(m.id)}</div>
          {/if}
          {#if s.error}
            <div class="row-error">{s.error}</div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  <footer class="footnote">
    <p>
      <strong>How the cascade works:</strong> when a project's MCP queries
      the hub for module state, the resolver checks the project's own row
      first; if none exists, it falls back to the host-wide default
      above; if that's also unset, modules are treated as enabled
      (fail-open).
    </p>
    <p>
      <strong>What these gate:</strong> whether a module is consulted for
      that project. Telemetry collection is separate and unaffected —
      retrieval events keep accumulating whether a module is on, off, or
      not installed at all.
    </p>
  </footer>
</div>

<style>
  .gmp {
    max-width: 760px;
    margin: 0 auto;
    padding: 1.5rem;
    color: var(--color-light, #e8e8ee);
  }
  header h2 {
    margin: 0 0 0.4rem 0;
    font-size: 1.3rem;
  }
  header .hint {
    margin: 0 0 1.2rem 0;
    color: var(--text-muted, #999);
    font-size: 0.9rem;
    line-height: 1.5;
  }
  header .hint em {
    font-style: italic;
    color: var(--color-light, #e8e8ee);
  }
  .loading {
    color: var(--text-muted, #999);
    padding: 1rem 0;
    font-size: 0.9rem;
  }
  ul.rows {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .row {
    background: var(--card-bg, rgba(255, 255, 255, 0.04));
    border: 1px solid var(--border, rgba(255, 255, 255, 0.08));
    border-radius: 8px;
    padding: 1rem;
  }
  .row.disabled {
    opacity: 0.6;
  }
  .row-main {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
  }
  .row-text {
    flex: 1;
  }
  .row-label {
    font-weight: 600;
    margin-bottom: 0.25rem;
  }
  .row-desc {
    font-size: 0.85rem;
    color: var(--text-muted, #999);
    line-height: 1.4;
    margin-bottom: 0.5rem;
  }
  .row-status {
    font-size: 0.85rem;
    color: var(--color-light, #e8e8ee);
  }
  .row-progress {
    font-size: 0.8rem;
    color: var(--accent, #00bfa6);
    margin-top: 0.25rem;
  }
  .row-controls {
    display: flex;
    gap: 0.4rem;
    flex-shrink: 0;
  }
  .toggle {
    background: rgba(255, 255, 255, 0.05);
    color: var(--color-light, #e8e8ee);
    border: 1px solid var(--border, rgba(255, 255, 255, 0.12));
    border-radius: 6px;
    padding: 0.45rem 0.9rem;
    font-size: 0.85rem;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }
  .toggle:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.1);
  }
  .toggle.active {
    background: var(--accent, #00bfa6);
    border-color: var(--accent, #00bfa6);
    color: #052e29;
    font-weight: 600;
  }
  .toggle:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }
  .row-error {
    margin-top: 0.5rem;
    color: var(--error, #ff6b6b);
    font-size: 0.8rem;
  }
  /* v0.2.91 (#23 USER rider): the module's switch is real but its effect is
     pending. Rendered as a quiet note, not a warning — nothing is broken. */
  .row-dormant {
    margin-top: 0.6rem;
    padding-left: 0.6rem;
    border-left: 2px solid var(--color-purple, #7b5fff);
    color: var(--text-muted, #999);
    font-size: 0.8rem;
    line-height: 1.45;
  }
  footer.footnote {
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border, rgba(255, 255, 255, 0.08));
    font-size: 0.8rem;
    color: var(--text-muted, #999);
    line-height: 1.5;
  }
</style>
