<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!--
  v0.2.101 (catalogue plan §3.6): the Packs tab — one row per opt-in
  agent/skill pack. The catalogue itself is Python's (vco_lib.packs reads
  templates/packs/packs.toml + the project manifest); this tab only lists
  it (`list_project_packs`) and toggles membership through the ORDINARY
  bundle engine (`set_project_pack_enabled` → install-bundle --update
  --pack / --remove-pack, single-flight per folder). Toggling re-populates
  project state, so members appear in / vanish from the Agents/Skills tabs
  immediately. A disabled toggle means another bundle engine run is in
  flight for this folder — never two engines on one project.
-->
<script lang="ts">
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { PackInfo } from '$lib/types/project-state';

  let { projectId }: { projectId: string } = $props();

  let packs = $state<PackInfo[]>([]);
  let loading = $state(true);
  // One toggle at a time (each toggle is a bundle-engine run).
  let busyPack = $state<string | null>(null);

  async function load() {
    loading = true;
    try {
      packs = await invoke<PackInfo[]>('list_project_packs', { projectId });
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  async function toggle(pack: string, enabled: boolean) {
    busyPack = pack;
    try {
      const warnings = await invoke<string[]>('set_project_pack_enabled', {
        projectId,
        pack,
        enabled,
      });
      // The command is synchronous — the engine run COMPLETED before this
      // toast (L3 review N-3), so past tense is the honest wording.
      toast.success(
        `Pack "${pack}" ${enabled ? 'installed' : 'removed'}`
          + (warnings.length > 0 ? ` (${warnings.length} warning(s))` : ''),
      );
      if (warnings.length > 0) console.warn('[packs] warnings:', warnings);
      await load();
    } catch (e) {
      toast.error(e);
      await load();
    } finally {
      busyPack = null;
    }
  }

  // One load per mount: the $effect fires on mount AND on projectId change
  // (N-2 — an onMount beside it double-fires the python status spawn).
  $effect(() => { if (projectId) void load(); });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Packs</h3>
  </header>
  <p class="ps-hint">
    Opt-in agent &amp; skill packs. Installing a pack delivers its members through the ordinary
    bundle update (they then appear in the Agents / Skills tabs and stay current on every
    "Update bundle"). Removing a pack deletes unmodified members and backs up any you edited to
    <code>.claude/backups/bundle-adoptions/</code> — your edits are never silently discarded.
    Only one bundle engine runs per project: if an "Update bundle" is already running elsewhere
    (e.g. Settings), a pack change waits for it to finish, so the click can take a while to
    complete.
  </p>

  {#if loading}
    <p class="ps-loading">Loading…</p>
  {:else if packs.length === 0}
    <div class="ps-empty-state">
      <p class="ps-empty">No packs available.</p>
      <p class="ps-empty-hint">
        The orchestrator clone ships the pack table at
        <code>templates/packs/packs.toml</code>. If it is missing, update the orchestrator
        install first.
      </p>
    </div>
  {:else}
    <table class="ps-table">
      <thead><tr><th>Pack</th><th>Description</th><th>Members</th><th>Installed</th></tr></thead>
      <tbody>
        {#each packs as p (p.name)}
          <tr>
            <td><code>{p.name}</code></td>
            <td>{p.description}</td>
            <td>
              <details>
                <summary>{p.members.length} member{p.members.length === 1 ? '' : 's'}</summary>
                <ul>
                  {#each p.members as m (m)}
                    <li><code>{m}</code></li>
                  {/each}
                </ul>
              </details>
            </td>
            <td>
              <label
                class="ps-tooltip"
                title="Installs via install-bundle --update --pack; removing backs up edited members before deleting"
              >
                <input
                  type="checkbox"
                  checked={p.installed}
                  disabled={busyPack !== null}
                  onchange={(e) => toggle(p.name, (e.target as HTMLInputElement).checked)}
                />
              </label>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</section>

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-hint { font-size: 11px; color: #888; margin: 0 0 16px; max-width: 720px; }
  .ps-hint code { background: rgba(255,255,255,0.08); padding: 1px 4px; border-radius: 3px; font-family: ui-monospace, monospace; }
  .ps-loading, .ps-empty { color: #888; padding: 24px; text-align: center; }
  .ps-empty-state { text-align: center; padding: 24px; }
  .ps-empty-state .ps-empty { padding: 0 0 8px; }
  .ps-empty-hint { color: #aaa; font-size: 12px; padding: 0 0 16px; max-width: 480px; margin: 0 auto; }
  .ps-empty-hint code { background: rgba(255,255,255,0.08); padding: 1px 4px; border-radius: 3px; font-family: ui-monospace, monospace; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 6px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: top; }
  .ps-table code { font-family: ui-monospace, monospace; font-size: 11px; }
  .ps-table details summary { cursor: pointer; color: #aaa; }
  .ps-table details ul { margin: 4px 0 0; padding-left: 16px; }
  .ps-table input:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-tooltip { cursor: help; }
</style>
