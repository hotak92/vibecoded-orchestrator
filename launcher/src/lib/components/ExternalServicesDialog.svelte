<script lang="ts">
  // ExternalServicesDialog — surfaces the list of services that the
  // launcher detected running on the canonical ports BUT didn't start
  // itself. The user picks adopt-vs-parallel-vs-cancel per service; the
  // choice is persisted to ~/.vct/services.toml so we don't re-prompt
  // every launcher boot.
  //
  // Triggered by the `vct-external-services-detected` event emitted by
  // `commands::lifecycle::auto_start_on_boot` (Rust side). Subscribed
  // from +layout.svelte so any route can show the dialog.

  import { onMount } from 'svelte';
  import { invoke, listen } from '$lib/tauri';

  interface ServiceRuntimeState {
    name: string;
    running: boolean;
    port: number;
    url: string;
    externally_managed: boolean;
    adoption_mode: 'unresolved' | 'adopt' | 'parallel' | 'refuse';
  }

  type Mode = 'adopt' | 'parallel' | 'refuse';

  let detected = $state<ServiceRuntimeState[]>([]);
  let open = $state(false);
  // Per-service user choice + parallel-port pick.
  let choices = $state<Record<string, Mode>>({});
  let parallelPorts = $state<Record<string, number>>({});
  let saving = $state(false);
  let error = $state<string | null>(null);

  // Suggested port ranges for parallel mode. We start above the canonical
  // ports + 10 to give headroom for users who already shifted ports.
  const parallelRanges: Record<string, [number, number]> = {
    weaviate: [8090, 8200],
    ollama: [11445, 11500],
    code_embed: [11450, 11500],
  };

  async function probeFreePort(name: string): Promise<number | null> {
    const range = parallelRanges[name];
    if (!range) return null;
    try {
      return await invoke<number>('services_find_free_port', {
        start: range[0],
        end: range[1],
      });
    } catch (e) {
      console.error(`[external-services-dialog] free port probe failed: ${e}`);
      return null;
    }
  }

  async function onModeChange(name: string, newMode: Mode) {
    choices[name] = newMode;
    if (newMode === 'parallel' && !parallelPorts[name]) {
      const port = await probeFreePort(name);
      if (port) parallelPorts[name] = port;
    }
  }

  async function applyChoices() {
    saving = true;
    error = null;
    try {
      for (const svc of detected) {
        const mode = choices[svc.name];
        if (!mode) continue;
        await invoke('services_set_adoption', {
          decision: {
            name: svc.name,
            mode,
            parallel_port: mode === 'parallel' ? parallelPorts[svc.name] : null,
            external_url: svc.url,
          },
        });
      }
      open = false;
    } catch (e) {
      error = String(e);
    } finally {
      saving = false;
    }
  }

  function cancel() {
    // Cancel = leave them all unresolved. The launcher will re-prompt
    // on next boot. We don't persist anything.
    open = false;
  }

  onMount(() => {
    let unlisten: (() => void) | null = null;
    listen<ServiceRuntimeState[]>('vct-external-services-detected', (e) => {
      detected = e.payload ?? [];
      // Default everyone to "adopt" — the safest no-op default. User
      // can flip to parallel/refuse before clicking Apply.
      const fresh: Record<string, Mode> = {};
      for (const svc of detected) fresh[svc.name] = 'adopt';
      choices = fresh;
      parallelPorts = {};
      open = detected.length > 0;
    }).then((u) => {
      unlisten = u;
    });
    return () => {
      if (unlisten) unlisten();
    };
  });
</script>

{#if open}
  <div class="dialog-backdrop" role="dialog" aria-modal="true" aria-labelledby="external-services-title">
    <div class="dialog-card">
      <h2 id="external-services-title">Existing services detected</h2>
      <p class="lead">
        VCT found services already running on this machine that weren't
        started by the launcher. Pick how you want to handle each one:
      </p>
      <ul class="service-list">
        {#each detected as svc}
          <li>
            <div class="svc-header">
              <strong>{svc.name}</strong>
              <span class="svc-url">{svc.url}</span>
            </div>
            <div class="svc-options">
              <label>
                <input
                  type="radio"
                  name="mode-{svc.name}"
                  value="adopt"
                  checked={choices[svc.name] === 'adopt'}
                  onchange={() => onModeChange(svc.name, 'adopt')}
                />
                Adopt — route to this existing endpoint as-is
              </label>
              <label>
                <input
                  type="radio"
                  name="mode-{svc.name}"
                  value="parallel"
                  checked={choices[svc.name] === 'parallel'}
                  onchange={() => onModeChange(svc.name, 'parallel')}
                />
                Run parallel on different port
                {#if choices[svc.name] === 'parallel'}
                  <input
                    type="number"
                    min="1024"
                    max="65535"
                    bind:value={parallelPorts[svc.name]}
                    placeholder="port"
                    class="port-input"
                  />
                {/if}
              </label>
              <label>
                <input
                  type="radio"
                  name="mode-{svc.name}"
                  value="refuse"
                  checked={choices[svc.name] === 'refuse'}
                  onchange={() => onModeChange(svc.name, 'refuse')}
                />
                Don't manage — keep handling this service yourself
              </label>
            </div>
          </li>
        {/each}
      </ul>
      {#if error}
        <p class="error">{error}</p>
      {/if}
      <div class="actions">
        <button onclick={cancel} disabled={saving}>Cancel</button>
        <button onclick={applyChoices} disabled={saving} class="primary">
          {saving ? 'Saving…' : 'Apply'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .dialog-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .dialog-card {
    background: var(--surface, #1a1a1a);
    color: var(--text, #f0f0f0);
    border-radius: 8px;
    padding: 1.5rem;
    width: min(640px, 92vw);
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  }
  h2 {
    margin: 0 0 0.5rem 0;
    font-size: 1.25rem;
  }
  .lead {
    margin: 0 0 1rem 0;
    color: var(--text-muted, #aaa);
    font-size: 0.9rem;
  }
  .service-list {
    list-style: none;
    padding: 0;
    margin: 0 0 1rem 0;
  }
  .service-list li {
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    padding: 0.75rem;
    margin-bottom: 0.5rem;
  }
  .svc-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 0.5rem;
  }
  .svc-url {
    font-family: monospace;
    font-size: 0.85rem;
    color: var(--text-muted, #aaa);
  }
  .svc-options {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .svc-options label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.9rem;
    cursor: pointer;
  }
  .port-input {
    width: 80px;
    padding: 0.2rem 0.4rem;
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    background: var(--input-bg, #0a0a0a);
    color: inherit;
    font-family: monospace;
  }
  .error {
    color: var(--error, #f87171);
    margin: 0.5rem 0;
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }
  button {
    padding: 0.5rem 1rem;
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    background: var(--button-bg, #2a2a2a);
    color: inherit;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  button.primary {
    background: var(--color-teal);
    border-color: var(--color-teal);
  }
</style>
