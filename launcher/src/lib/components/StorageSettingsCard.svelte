<script lang="ts">
  /**
   * PR-10A storage settings card.
   *
   * Surfaces:
   *  - Radio: Named volume (recommended) | Bind mount (custom path)
   *  - Per-service path override grid (visible only in bind mode)
   *  - "Detected pre-existing volumes" panel (visible only if non-empty)
   *    with a "Use this for <service>" action per volume
   *  - Apply button (calls set_storage_config), with a "containers must
   *    restart" hint
   *
   * STRICT allowlist note: the Rust side filters `detect_legacy_volumes`
   * through a hand-curated allowlist + the `vco_*` prefix. We never get
   * to see sibling-project volumes (aihive-*, artup_*, bitmagnet-*, etc.)
   * here — the FE just renders whatever the backend hands back.
   */
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { pickDirectory } from '$lib/dialog';
  import { toast } from '$lib/stores/toast';

  type StorageMode = 'named' | 'bind';

  interface StorageConfig {
    mode: StorageMode;
    bind_root: string;
    per_service_paths: Record<string, string>;
    external_aliases: Record<string, string>;
  }

  interface DetectedLegacyVolume {
    name: string;
    mountpoint: string;
    driver: string;
    role: string;
  }

  interface StorageConfigView {
    config: StorageConfig;
    config_path: string;
    legacy_volumes: DetectedLegacyVolume[];
    synthesized_from_defaults: boolean;
  }

  // The three orchestrator services that own named volumes. Order
  // mirrors `storage_ux::LOGICAL_SERVICES`. Keep the labels neutral —
  // these are the technical names, not marketing.
  const SERVICES: { key: string; label: string; hint: string }[] = [
    { key: 'weaviate', label: 'Weaviate (KG vector DB)', hint: 'Vector store for the knowledge graph' },
    { key: 'ollama', label: 'Ollama (local LLM models)', hint: 'Downloaded model weights live here' },
    { key: 'code_embed', label: 'Code-embed cache', hint: 'Hugging Face model cache for code embeddings' },
  ];

  let loading = $state(true);
  let saving = $state(false);
  let view = $state<StorageConfigView | null>(null);

  // Editable form state (forked from view.config when we load)
  let mode = $state<StorageMode>('named');
  let bindRoot = $state('');
  let perServicePaths = $state<Record<string, string>>({});
  let externalAliases = $state<Record<string, string>>({});

  async function load() {
    loading = true;
    try {
      const v = await invoke<StorageConfigView>('get_storage_config');
      view = v;
      mode = (v.config.mode as StorageMode) ?? 'named';
      bindRoot = v.config.bind_root ?? '';
      perServicePaths = { ...(v.config.per_service_paths ?? {}) };
      externalAliases = { ...(v.config.external_aliases ?? {}) };
    } catch (err) {
      toast.error(`Failed to load storage config: ${err}`);
    } finally {
      loading = false;
    }
  }

  onMount(load);

  async function browseBindRoot() {
    const picked = await pickDirectory({
      title: 'Choose a folder to hold VCO container data',
    });
    if (picked) {
      bindRoot = picked;
    }
  }

  async function browseServicePath(service: string) {
    const picked = await pickDirectory({
      title: `Choose a folder for ${service}`,
    });
    if (picked) {
      perServicePaths = { ...perServicePaths, [service]: picked };
    }
  }

  function clearServiceOverride(service: string) {
    const next = { ...perServicePaths };
    delete next[service];
    perServicePaths = next;
  }

  function useVolumeForService(volume: DetectedLegacyVolume, service: string) {
    // Map service -> canonical compose volume key. Mirrors
    // `storage_ux::canonical_volume_for`. The backend validates again
    // before writing.
    const canonical =
      service === 'weaviate' ? 'weaviate_data'
      : service === 'ollama' ? 'ollama_data'
      : service === 'code_embed' ? 'code_embed_cache'
      : null;
    if (!canonical) {
      toast.error(`Unknown service: ${service}`);
      return;
    }
    externalAliases = { ...externalAliases, [canonical]: volume.name };
    toast.info(`Aliased ${canonical} -> ${volume.name}. Click Apply to save.`);
  }

  function clearAlias(canonical: string) {
    const next = { ...externalAliases };
    delete next[canonical];
    externalAliases = next;
  }

  async function apply() {
    saving = true;
    try {
      const next: StorageConfig = {
        mode,
        bind_root: bindRoot,
        per_service_paths: perServicePaths,
        external_aliases: externalAliases,
      };
      const updated = await invoke<StorageConfigView>('set_storage_config', {
        config: next,
      });
      view = updated;
      toast.success(
        'Storage configuration saved. Restart containers (Services -> Restart all) to apply.',
      );
    } catch (err) {
      toast.error(`Failed to save: ${err}`);
    } finally {
      saving = false;
    }
  }

  function roleLabel(role: string): string {
    switch (role) {
      case 'weaviate': return 'Weaviate KG';
      case 'ollama': return 'Ollama models';
      case 'code_embed': return 'Code-embed cache';
      case 'searxng': return 'SearXNG';
      case 'neo4j': return 'Neo4j';
      case 'model_router': return 'Model router';
      default: return 'Other';
    }
  }

  // Are any external aliases pinned right now?
  const aliasKeys = $derived(Object.keys(externalAliases));
</script>

<section class="storage-card">
  <header>
    <h2>Storage location</h2>
    <p class="lede">
      Choose where the orchestrator keeps its container data
      (Weaviate KG, Ollama model weights, code-embed cache).
    </p>
  </header>

  {#if loading}
    <p class="muted">Loading…</p>
  {:else if view}
    <fieldset class="mode-group">
      <legend>Mode</legend>
      <label class="radio-row">
        <input type="radio" bind:group={mode} value="named" />
        <span class="radio-label">
          <strong>Named volume (recommended)</strong>
          <span class="sub">
            Managed by the container runtime. Fast on all OSes, automatic
            permissions, opaque path. Best for most users.
          </span>
        </span>
      </label>
      <label class="radio-row">
        <input type="radio" bind:group={mode} value="bind" />
        <span class="radio-label">
          <strong>Custom path (bind mount)</strong>
          <span class="sub">
            Transparent on disk, backup-friendly. Slower on macOS / Windows
            Docker Desktop. Recommended if you already back up a specific
            folder.
          </span>
        </span>
      </label>
    </fieldset>

    {#if mode === 'bind'}
      <fieldset class="bind-form">
        <legend>Custom path</legend>
        <div class="path-row">
          <label class="path-label" for="bind-root">
            Root folder
          </label>
          <div class="path-input-wrap">
            <input
              id="bind-root"
              type="text"
              bind:value={bindRoot}
              placeholder="(absolute path)"
              spellcheck="false"
              autocomplete="off"
            />
            <button type="button" class="browse" onclick={browseBindRoot}>
              Browse…
            </button>
          </div>
          <p class="hint">
            The launcher will create sub-folders under this path
            (one per service: <code>weaviate/</code>, <code>ollama/</code>,
            <code>code_embed/</code>). Per-service overrides below take
            precedence.
          </p>
        </div>

        <h3 class="sub-h">Per-service overrides (optional)</h3>
        <p class="hint">
          Pin one service onto a faster disk while leaving others on the
          system disk.
        </p>
        <table class="svc-grid">
          <thead>
            <tr>
              <th>Service</th>
              <th>Path</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {#each SERVICES as svc}
              <tr>
                <td>
                  <strong>{svc.label}</strong>
                  <div class="hint">{svc.hint}</div>
                </td>
                <td>
                  <div class="path-input-wrap small">
                    <input
                      type="text"
                      placeholder="(use root)"
                      value={perServicePaths[svc.key] ?? ''}
                      oninput={(e) => {
                        const val = (e.currentTarget as HTMLInputElement).value;
                        if (val.trim() === '') {
                          clearServiceOverride(svc.key);
                        } else {
                          perServicePaths = {
                            ...perServicePaths,
                            [svc.key]: val,
                          };
                        }
                      }}
                      spellcheck="false"
                      autocomplete="off"
                    />
                    <button
                      type="button"
                      class="browse small"
                      onclick={() => browseServicePath(svc.key)}
                    >
                      Browse…
                    </button>
                  </div>
                </td>
                <td>
                  {#if perServicePaths[svc.key]}
                    <button
                      type="button"
                      class="link"
                      onclick={() => clearServiceOverride(svc.key)}
                    >
                      Clear
                    </button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </fieldset>
    {/if}

    {#if view.legacy_volumes.length > 0}
      <fieldset class="legacy-form">
        <legend>Detected pre-existing volumes</legend>
        <p class="hint">
          We found these volumes already on this machine that look like
          VCO data (filtered through the strict allowlist — we never
          show data from unrelated projects). Reuse one by clicking
          "Use this for …" — your existing data stays in place.
        </p>
        <table class="vol-grid">
          <thead>
            <tr>
              <th>Volume name</th>
              <th>Role</th>
              <th>Mountpoint</th>
              <th>Use this for</th>
            </tr>
          </thead>
          <tbody>
            {#each view.legacy_volumes as vol}
              <tr>
                <td><code>{vol.name}</code></td>
                <td>{roleLabel(vol.role)}</td>
                <td>
                  <code class="path">{vol.mountpoint || '(unavailable)'}</code>
                </td>
                <td>
                  <div class="use-buttons">
                    {#each SERVICES as svc}
                      <button
                        type="button"
                        class="small"
                        onclick={() => useVolumeForService(vol, svc.key)}
                      >
                        {svc.key}
                      </button>
                    {/each}
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </fieldset>
    {/if}

    {#if aliasKeys.length > 0}
      <fieldset class="alias-form">
        <legend>Active external aliases</legend>
        <p class="hint">
          These pin the canonical compose volume to an existing host-side
          named volume. Saving will emit a <code>external: true</code>
          entry in <code>infrastructure/docker-compose.override.yml</code>.
        </p>
        <ul class="alias-list">
          {#each aliasKeys as canonical}
            <li>
              <code>{canonical}</code> &rarr; <code>{externalAliases[canonical]}</code>
              <button
                type="button"
                class="link"
                onclick={() => clearAlias(canonical)}
              >
                Remove
              </button>
            </li>
          {/each}
        </ul>
      </fieldset>
    {/if}

    <footer class="actions">
      <div class="path-info">
        <span class="muted">Config file:</span>
        <code class="path">{view.config_path}</code>
      </div>
      <button
        type="button"
        class="primary"
        disabled={saving}
        onclick={apply}
      >
        {saving ? 'Saving…' : 'Apply'}
      </button>
    </footer>
    <p class="hint footer-hint">
      After saving, restart containers via Services to pick up the new
      storage configuration. Existing data is not moved automatically —
      use the per-volume migration helpers in the docs to rsync data
      between paths.
    </p>
  {/if}
</section>

<style>
  /* v0.2.46: replaced undefined --bg-* and --text-* tokens (which all fell
   * through to light-mode hex fallbacks like #fff / #1a1a1a / #f9f9fb,
   * producing an unreadable white card on this app's dark theme) with
   * the same colour vocabulary the rest of the GUI uses — the `ps-*`
   * recipe shared across IdentityTab / SecretsTab / SettingsTab /
   * KgCodegraphTab / SkillsTab / HooksTab. No new tokens, no one-off
   * button styling: just the canonical app values.
   *
   * Canonical recipe (do NOT diverge from this in new panels):
   *   - Card surface       : rgba(255,255,255,0.03), radius 6px
   *   - Border             : 1px solid rgba(255,255,255,0.12)
   *   - Text               : inherit (= --color-text from app.css)
   *   - Muted/hint text    : #888
   *   - Sub-hint text      : #777
   *   - Section heading h4 : #c4b3ff (purple-tinted accent)
   *   - Input              : bg rgba(255,255,255,0.05), border (border above)
   *   - Primary button     : bg rgb(0,191,166), color #000, padding 6px 14px
   *   - Secondary button   : bg rgba(255,255,255,0.05), color inherit, border (border above)
   *   - Code inline        : bg rgba(255,255,255,0.05), padding 1px 4px
   *
   * Native <select> dropdowns are styled globally by `app.css` —
   * `color-scheme: dark` + explicit `select`/`select option` rules —
   * so they are NOT overridden here. (Lesson from 0d540b6
   * "fix(secrets): dropdown styling".)
   */

  .storage-card {
    color: inherit;
    border-radius: 8px;
    padding: 1.5rem;
    max-width: 920px;
  }

  header h2 {
    margin: 0 0 0.25rem 0;
    font-size: 1.4rem;
  }

  .lede {
    color: #888;
    margin: 0 0 1.5rem 0;
    font-size: 13px;
    line-height: 1.4;
  }

  fieldset {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 6px;
    padding: 14px 16px;
    margin: 0 0 1.25rem 0;
  }

  legend {
    padding: 0 0.5rem;
    font-weight: 600;
    color: #c4b3ff;
    font-size: 13px;
  }

  .mode-group .radio-row {
    display: flex;
    align-items: flex-start;
    gap: 0.6rem;
    padding: 0.6rem;
    border-radius: 4px;
    cursor: pointer;
  }

  .mode-group .radio-row:hover {
    background: rgba(255,255,255,0.05);
  }

  .radio-label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .radio-label .sub {
    color: #888;
    font-size: 11px;
  }

  .path-row {
    margin-bottom: 1rem;
  }

  .path-label {
    display: block;
    font-weight: 500;
    margin-bottom: 0.25rem;
    font-size: 11px;
    color: #888;
  }

  .path-input-wrap {
    display: flex;
    gap: 0.5rem;
  }

  .path-input-wrap input {
    flex: 1;
    padding: 6px 10px;
    background: rgba(255,255,255,0.05);
    color: inherit;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 4px;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 13px;
  }

  .path-input-wrap input:focus-visible {
    outline: none;
    border-color: rgba(0,191,166,0.55);
    box-shadow: 0 0 0 3px rgba(0,191,166,0.10);
  }

  .path-input-wrap.small input {
    padding: 0.3rem 0.5rem;
  }

  /* Secondary button — matches .ps-btn-secondary across the rest of the GUI. */
  button.browse {
    padding: 6px 14px;
    background: rgba(255,255,255,0.05);
    color: inherit;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
  }

  button.browse:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }

  button.browse:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  button.browse.small {
    padding: 4px 10px;
    font-size: 11px;
  }

  .sub-h {
    font-size: 13px;
    margin: 1rem 0 0.25rem 0;
    color: #c4b3ff;
  }

  .hint {
    font-size: 11px;
    color: #777;
    margin: 0.25rem 0;
    line-height: 1.4;
  }

  .svc-grid,
  .vol-grid {
    width: 100%;
    border-collapse: collapse;
    margin-top: 0.5rem;
  }

  .svc-grid th,
  .svc-grid td,
  .vol-grid th,
  .vol-grid td {
    text-align: left;
    padding: 0.45rem 0.5rem;
    vertical-align: top;
    border-bottom: 1px solid rgba(255,255,255,0.08);
  }

  .svc-grid th,
  .vol-grid th {
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    font-weight: 600;
    font-size: 11px;
  }

  .vol-grid code {
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    font-size: 12px;
    background: rgba(255,255,255,0.05);
    padding: 2px 6px;
    border-radius: 3px;
  }

  .vol-grid code.path {
    word-break: break-all;
    color: #888;
    background: rgba(255,255,255,0.05);
  }

  .use-buttons {
    display: flex;
    gap: 0.3rem;
    flex-wrap: wrap;
  }

  /* Pills — also use the .ps-btn-secondary recipe, slimmer padding. */
  .use-buttons button {
    padding: 3px 10px;
    font-size: 11px;
    background: rgba(255,255,255,0.05);
    color: inherit;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 4px;
    cursor: pointer;
  }

  .use-buttons button:hover:not(:disabled) {
    background: rgba(255,255,255,0.08);
  }

  .alias-list {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0 0 0;
  }

  .alias-list li {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.35rem 0;
    border-bottom: 1px solid rgba(255,255,255,0.08);
  }

  button.link {
    background: transparent;
    border: none;
    color: rgb(0,191,166);
    cursor: pointer;
    padding: 0;
    font-size: 12px;
  }

  button.link:hover {
    color: rgb(0,212,184);
  }

  .actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-top: 1rem;
  }

  .path-info {
    font-size: 11px;
    color: #777;
  }

  .path-info code.path {
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    word-break: break-all;
    background: rgba(255,255,255,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    color: #888;
  }

  /* Primary button — matches .ps-btn-primary across the rest of the GUI. */
  button.primary {
    padding: 6px 14px;
    background: rgb(0,191,166);
    color: #000;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }

  button.primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .footer-hint {
    margin-top: 0.75rem;
  }

  .muted {
    color: #888;
  }

  code {
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  }
</style>
