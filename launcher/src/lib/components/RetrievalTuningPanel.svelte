<script lang="ts">
  /**
   * RetrievalTuningPanel.svelte — global thresholds for KG retrieval
   * tiering + codegraph injection floor.
   *
   * v0.2.22 Item #13 (2026-05-20). Surfaces five env-tunable knobs the
   * orchestrator's retrieval pipelines already honour:
   *   - VCO_CODE_GRAPH_SCORE_FLOOR — pre-edit codegraph injection cutoff
   *   - KG_TIER_MIN / SINGLE_CHUNK / THREE_CHUNKS / FULL — score-driven
   *     verbosity tiers from knowledge/concepts/score-driven-retrieval-tiers.md
   *
   * Persistence: <vct_root_dir>/retrieval-tuning.toml via the Tauri
   * commands `retrieval_tuning_get` / `retrieval_tuning_set` /
   * `retrieval_tuning_reset`. The hub's /config resolver reads the
   * same file so headless clients (Python / bash) see what's shown
   * here.
   *
   * Invariant: kg_tier_min < kg_tier_single_chunk < kg_tier_three_chunks
   *            < kg_tier_full  (strict).
   *
   * Validation is client-side AND server-side; the Rust command is the
   * authoritative gate. We do NOT auto-clamp — when the user drags a
   * slider out of order we surface an inline error and refuse the save.
   *
   * Every control has a mouseover tooltip (Dev Constraint per the
   * v0.2.22 plan: "all controls have mouseover tooltips").
   */
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';

  /** Wire shape — matches the Rust `RetrievalTuning` struct exactly. */
  interface RetrievalTuning {
    code_graph_score_floor: number;
    kg_tier_min: number;
    kg_tier_single_chunk: number;
    kg_tier_three_chunks: number;
    kg_tier_full: number;
  }

  /**
   * Calibrated defaults from
   * `knowledge/concepts/score-driven-retrieval-tiers.md`. Pinned here
   * for the "reset to default" buttons. The Rust side ALSO knows the
   * defaults (and is the authoritative source — the unit tests there
   * pin them); we duplicate so the FE can show the default value in
   * the per-slider tooltip even before the first round-trip completes.
   */
  const DEFAULTS: RetrievalTuning = {
    code_graph_score_floor: 0.35,
    kg_tier_min: 0.42,
    kg_tier_single_chunk: 0.55,
    kg_tier_three_chunks: 0.65,
    kg_tier_full: 0.75,
  };

  /**
   * Per-knob metadata table. Co-located with the wire shape so adding
   * a new knob is a one-place edit (Rust struct + this table + Tauri
   * command name).
   */
  interface KnobMeta {
    key: keyof RetrievalTuning;
    label: string;
    description: string;
    tooltip: string;
    docAnchor: string;
  }

  const KNOBS: KnobMeta[] = [
    {
      key: 'code_graph_score_floor',
      label: 'Codegraph score floor',
      description:
        'Minimum cosine score for codegraph hits to be injected into the pre-edit context.',
      tooltip:
        'Pre-edit hook discards codegraph search results scoring below this value. Higher = fewer but tighter hits. Independent of KG tier thresholds.',
      docAnchor: 'VCO_CODE_GRAPH_SCORE_FLOOR',
    },
    {
      key: 'kg_tier_min',
      label: 'KG: discard floor',
      description:
        'KG search results scoring below this value are dropped entirely (treated as noise).',
      tooltip:
        'Below this score → discard. Above → render at the summary tier. Must be < kg_tier_single_chunk.',
      docAnchor: 'KG_TIER_MIN',
    },
    {
      key: 'kg_tier_single_chunk',
      label: 'KG: single-chunk threshold',
      description:
        'Above this score, render the matched chunk (~2000 chars) instead of just the summary.',
      tooltip:
        'Above this → render the matched chunk only. Must sit between kg_tier_min and kg_tier_three_chunks.',
      docAnchor: 'KG_TIER_SINGLE_CHUNK',
    },
    {
      key: 'kg_tier_three_chunks',
      label: 'KG: three-chunks threshold',
      description:
        'Above this score, render the matched chunk plus its two neighbours.',
      tooltip:
        'Above this → render 3 chunks (matched + 2 neighbours). Must sit between kg_tier_single_chunk and kg_tier_full.',
      docAnchor: 'KG_TIER_THREE_CHUNKS',
    },
    {
      key: 'kg_tier_full',
      label: 'KG: full-node threshold',
      description:
        'Above this score, render the whole KG node (up to 7 nearest chunks).',
      tooltip:
        'Above this → render the whole node (capped at 7 chunks). Must be > kg_tier_three_chunks.',
      docAnchor: 'KG_TIER_FULL',
    },
  ];

  /** Current values (loaded from the backend on mount). */
  let values = $state<RetrievalTuning>({ ...DEFAULTS });
  let loading = $state(true);
  let saving = $state(false);
  /** Last successfully-saved snapshot — used to detect "dirty" state. */
  let pristine = $state<RetrievalTuning>({ ...DEFAULTS });

  /**
   * Validate the in-memory values against the ordering + range
   * invariant. Returns `null` on success or a human-readable error
   * string on failure. Match the Rust validator's behaviour
   * (fail-fast — return the first violation found).
   */
  function validate(t: RetrievalTuning): string | null {
    const fields: Array<[keyof RetrievalTuning, number]> = [
      ['code_graph_score_floor', t.code_graph_score_floor],
      ['kg_tier_min', t.kg_tier_min],
      ['kg_tier_single_chunk', t.kg_tier_single_chunk],
      ['kg_tier_three_chunks', t.kg_tier_three_chunks],
      ['kg_tier_full', t.kg_tier_full],
    ];
    for (const [name, val] of fields) {
      if (!Number.isFinite(val)) return `${name} is not a finite number`;
      if (val < 0 || val > 1) return `${name} must be in [0, 1] (got ${val})`;
    }
    if (!(t.kg_tier_min < t.kg_tier_single_chunk)) {
      return 'kg_tier_min must be strictly less than kg_tier_single_chunk';
    }
    if (!(t.kg_tier_single_chunk < t.kg_tier_three_chunks)) {
      return 'kg_tier_single_chunk must be strictly less than kg_tier_three_chunks';
    }
    if (!(t.kg_tier_three_chunks < t.kg_tier_full)) {
      return 'kg_tier_three_chunks must be strictly less than kg_tier_full';
    }
    return null;
  }

  /** Live-derived validation error — drives the inline error banner. */
  const validationError = $derived(validate(values));

  /** Live-derived dirty flag — drives the Save button's disabled state. */
  const isDirty = $derived(
    KNOBS.some((k) => values[k.key] !== pristine[k.key]),
  );

  async function load() {
    loading = true;
    try {
      const got = await invoke<RetrievalTuning>('retrieval_tuning_get');
      values = { ...got };
      pristine = { ...got };
    } catch (e) {
      toast.error(`Failed to load retrieval tuning: ${e}`);
    } finally {
      loading = false;
    }
  }

  async function save() {
    const err = validate(values);
    if (err) {
      toast.error(`Cannot save: ${err}`);
      return;
    }
    saving = true;
    try {
      await invoke('retrieval_tuning_set', { tuning: values });
      pristine = { ...values };
      toast.success('Retrieval tuning saved');
    } catch (e) {
      // Backend rejection is authoritative — surface verbatim so the
      // user sees the same wording in toast + inline banner.
      toast.error(String(e));
    } finally {
      saving = false;
    }
  }

  async function resetAll() {
    if (!confirm(
      'Reset all retrieval thresholds to their calibrated defaults?\n\n' +
        'This overwrites <vct_root_dir>/retrieval-tuning.toml. Headless ' +
        'consumers (hooks, MCPs) will see the new values on their next call.',
    )) {
      return;
    }
    saving = true;
    try {
      const defaults = await invoke<RetrievalTuning>('retrieval_tuning_reset');
      values = { ...defaults };
      pristine = { ...defaults };
      toast.success('Reset to defaults');
    } catch (e) {
      toast.error(String(e));
    } finally {
      saving = false;
    }
  }

  /** Reset a single knob without round-tripping the backend. */
  function resetOne(key: keyof RetrievalTuning) {
    values = { ...values, [key]: DEFAULTS[key] };
  }

  function onSliderInput(key: keyof RetrievalTuning, raw: string) {
    const num = Number(raw);
    if (!Number.isFinite(num)) return;
    values = { ...values, [key]: num };
  }

  function onNumberInput(key: keyof RetrievalTuning, raw: string) {
    // Allow empty intermediate value while typing (don't clobber to 0)
    // — but on blur the browser's `number` input will commit a real
    // value. We only update state once parsing succeeds.
    const num = Number(raw);
    if (!Number.isFinite(num)) return;
    values = { ...values, [key]: num };
  }

  onMount(() => {
    void load();
  });
</script>

<section class="rt-panel" aria-labelledby="rt-title">
  <header class="rt-header">
    <h2 id="rt-title" class="rt-title">Retrieval tuning</h2>
    <p class="rt-hint">
      Global thresholds for score-driven KG verbosity (4 tiers) and
      codegraph injection floor (1 cutoff). Stored at
      <code>&lt;vct_root_dir&gt;/retrieval-tuning.toml</code>; the
      hub's <code>/api/v1/projects/&lt;id&gt;/config</code> resolver
      reads the same file so hooks and headless MCPs see the same
      values shown here.
    </p>
    <p class="rt-hint">
      Reference:
      <code>knowledge/concepts/score-driven-retrieval-tiers.md</code>
      (calibrated 2026-04-10).
    </p>
  </header>

  {#if loading}
    <p class="rt-empty">Loading…</p>
  {:else}
    <ul class="rt-list">
      {#each KNOBS as knob}
        {@const value = values[knob.key]}
        {@const isDefault = value === DEFAULTS[knob.key]}
        <li class="rt-row" title={knob.tooltip}>
          <div class="rt-row-label">
            <strong>{knob.label}</strong>
            <span class="rt-row-desc">{knob.description}</span>
            <span class="rt-row-env">
              env: <code>{knob.docAnchor}</code> · default
              <code>{DEFAULTS[knob.key].toFixed(2)}</code>
            </span>
          </div>
          <div class="rt-row-controls">
            <input
              class="rt-slider"
              type="range"
              min="0"
              max="1"
              step="0.01"
              value={value}
              aria-label={knob.label}
              title={knob.tooltip}
              oninput={(e) =>
                onSliderInput(
                  knob.key,
                  (e.target as HTMLInputElement).value,
                )}
            />
            <input
              class="rt-number"
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={value}
              aria-label={`${knob.label} numeric value`}
              title={knob.tooltip}
              oninput={(e) =>
                onNumberInput(
                  knob.key,
                  (e.target as HTMLInputElement).value,
                )}
            />
            <button
              class="rt-reset-one"
              type="button"
              title="Reset this threshold to its calibrated default"
              disabled={isDefault}
              onclick={() => resetOne(knob.key)}
            >
              ↺
            </button>
          </div>
        </li>
      {/each}
    </ul>

    {#if validationError}
      <p class="rt-error" role="alert">
        <strong>Cannot save:</strong>
        {validationError}
      </p>
    {/if}

    <div class="rt-actions">
      <button
        class="rt-btn-secondary"
        type="button"
        title="Reset every threshold to the calibrated defaults shipped with the orchestrator"
        disabled={saving || loading}
        onclick={() => void resetAll()}
      >
        Reset all to defaults
      </button>
      <button
        class="rt-btn-primary"
        type="button"
        title={validationError
          ? `Fix validation first: ${validationError}`
          : 'Persist these values to <vct_root_dir>/retrieval-tuning.toml'}
        disabled={
          !isDirty || saving || loading || validationError !== null
        }
        onclick={() => void save()}
      >
        {saving ? 'Saving…' : 'Save'}
      </button>
    </div>
  {/if}
</section>

<style>
  .rt-panel {
    background: var(--surface-1, #1c1c1c);
    border: 1px solid var(--border-1, #2a2a2a);
    border-radius: 8px;
    padding: 1.25rem 1.5rem;
    margin: 1rem 0;
  }

  .rt-header {
    margin-bottom: 1rem;
  }

  .rt-title {
    margin: 0 0 0.5rem 0;
    font-size: 1.1rem;
    font-weight: 600;
  }

  .rt-hint {
    margin: 0.25rem 0;
    color: var(--text-2, #aaa);
    font-size: 0.85rem;
    line-height: 1.4;
  }

  .rt-hint code {
    background: var(--surface-2, #232323);
    padding: 0.05em 0.35em;
    border-radius: 3px;
    font-size: 0.9em;
  }

  .rt-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .rt-row {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 0.5rem 1rem;
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--border-2, #232323);
  }

  .rt-row:last-child {
    border-bottom: none;
  }

  .rt-row-label {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }

  .rt-row-label strong {
    font-size: 0.95rem;
  }

  .rt-row-desc {
    color: var(--text-2, #aaa);
    font-size: 0.82rem;
    line-height: 1.35;
  }

  .rt-row-env {
    color: var(--text-3, #888);
    font-size: 0.78rem;
  }

  .rt-row-env code {
    background: var(--surface-2, #232323);
    padding: 0 0.3em;
    border-radius: 3px;
  }

  .rt-row-controls {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .rt-slider {
    width: 180px;
  }

  .rt-number {
    width: 5.5em;
    padding: 0.25rem 0.4rem;
    background: var(--surface-2, #232323);
    color: var(--text-1, #e0e0e0);
    border: 1px solid var(--border-1, #2a2a2a);
    border-radius: 4px;
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: 0.85rem;
  }

  .rt-reset-one {
    background: transparent;
    color: var(--text-2, #aaa);
    border: 1px solid var(--border-1, #2a2a2a);
    border-radius: 4px;
    padding: 0.2rem 0.45rem;
    font-size: 0.95rem;
    cursor: pointer;
    line-height: 1;
  }

  .rt-reset-one:hover:not(:disabled) {
    background: var(--surface-2, #232323);
  }

  .rt-reset-one:disabled {
    opacity: 0.35;
    cursor: default;
  }

  .rt-error {
    margin: 0.75rem 0 0 0;
    padding: 0.5rem 0.75rem;
    background: rgba(220, 80, 80, 0.12);
    border: 1px solid rgba(220, 80, 80, 0.45);
    border-radius: 4px;
    color: #f3a0a0;
    font-size: 0.85rem;
  }

  .rt-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin-top: 1rem;
  }

  .rt-btn-primary,
  .rt-btn-secondary {
    padding: 0.4rem 0.9rem;
    border-radius: 4px;
    font-size: 0.88rem;
    cursor: pointer;
    border: 1px solid var(--border-1, #2a2a2a);
  }

  .rt-btn-primary {
    background: var(--accent, #3b82f6);
    color: #fff;
    border-color: var(--accent, #3b82f6);
  }

  .rt-btn-primary:hover:not(:disabled) {
    filter: brightness(1.08);
  }

  .rt-btn-primary:disabled {
    background: var(--surface-2, #232323);
    color: var(--text-3, #666);
    border-color: var(--border-1, #2a2a2a);
    cursor: default;
  }

  .rt-btn-secondary {
    background: var(--surface-2, #232323);
    color: var(--text-1, #e0e0e0);
  }

  .rt-btn-secondary:hover:not(:disabled) {
    background: var(--surface-3, #2d2d2d);
  }

  .rt-btn-secondary:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .rt-empty {
    color: var(--text-2, #aaa);
    font-style: italic;
    margin: 0.5rem 0;
  }
</style>
