<script lang="ts">
  // v0.2.46 V47-G-final: Adopt-project modal for the Add-Project wizard.
  //
  // ── What this is ────────────────────────────────────────────────────
  // When the user picks a directory in Add Project that contains
  // existing-project signals (CLAUDE.md / .env / .venv/ / .claude/ /
  // knowledge/) but is NOT already a VCO-managed project (no
  // .vco-manifest.json), this modal pops up offering:
  //
  //   1) Adopt (recommended)  — install with --adopt-project (preserves
  //                             pre-existing files via .vco-new siblings)
  //   2) Cancel               — bail out, don't change anything
  //   3) Show details         — expand the signal list with verbose
  //                             per-signal descriptions
  //
  // Mirrors the CLI prompt rendered by `_prompt_adopt_decision` in
  // install.py. The Rust-side `detect_third_party_project_signals` Tauri
  // command produces the signal list (cheap mirror of the canonical
  // Python heuristic — install.py runs the real detection again at
  // install time).
  //
  // ── Contract ────────────────────────────────────────────────────────
  //   props.detection — ThirdPartyDetection { has_signals, manifest_present,
  //                     signals[], summary }
  //   props.installPath — display in the modal header
  //   props.onAdopt   — called when user picks "Adopt"
  //   props.onCancel  — called when user picks "Cancel" or closes the modal
  //
  // Both callbacks are sync; the parent component is responsible for
  // invoking install.py with --adopt-project / no-adopt-project.

  import DialogRoot from '$lib/components/DialogRoot.svelte';

  export type ThirdPartyDetection = {
    has_signals: boolean;
    manifest_present: boolean;
    signals: string[];
    summary: string;
  };

  let {
    open = $bindable(false),
    detection,
    installPath,
    onAdopt,
    onCancel,
  }: {
    open: boolean;
    detection: ThirdPartyDetection;
    installPath: string;
    onAdopt: () => void;
    onCancel: () => void;
  } = $props();

  let showDetails = $state(false);

  function handleAdopt() {
    showDetails = false;
    onAdopt();
  }
  function handleCancel() {
    showDetails = false;
    onCancel();
  }
</script>

<DialogRoot bind:open onClose={handleCancel} ariaLabel="Existing project detected">
  {#snippet header()}
    <h2 class="adopt-title">Existing project detected</h2>
  {/snippet}
  {#snippet body()}
    <div class="adopt-modal">
      <p class="adopt-path">
        <code>{installPath}</code> contains:
      </p>

      <ul class="adopt-signals">
        {#each detection.signals as sig (sig)}
          <li>{sig}</li>
        {/each}
      </ul>

      <p class="adopt-question">
        <strong>Adopt this project under VCO?</strong>
      </p>

      <p class="adopt-explanation">
        Adopt mode is <em>maximally protective</em>: existing files are
        preserved, VCO defaults land as <code>.vco-new</code> siblings, and
        deferral entries are generated for everything that needs your
        review. Nothing in this folder is overwritten.
      </p>

      {#if showDetails}
        <div class="adopt-details">
          <h4>What each signal means</h4>
          <ul>
            {#each detection.signals as sig (sig)}
              <li>
                <code>{sig}</code>
                <p class="adopt-detail-text">
                  {#if sig.startsWith('.claude/')}
                    Your existing <code>.claude/</code> artifacts are kept.
                    VCO's agents/skills/hooks land alongside, never on top.
                  {:else if sig.startsWith('CLAUDE.md')}
                    Your project instructions are appended to (not
                    replaced) with the VCO orchestrator block. The original
                    content stays at the top.
                  {:else if sig.startsWith('.env')}
                    V47-C audits secret-shaped keys and offers to migrate
                    them to the OS keychain (Y/n/details prompt). Your
                    <code>.env</code> file is rewritten only if you accept.
                  {:else if sig.includes('/ (Python virtualenv)') || sig.includes('venv')}
                    V47-D preserves your venv (skip-no-manifest action).
                    Pass <code>--rebuild-venv</code> only if you want VCO's
                    default venv instead.
                  {:else if sig.startsWith('knowledge/')}
                    Your knowledge directory is indexed by VCO's KG layer.
                    Nothing is overwritten or moved.
                  {:else}
                    Existing content preserved.
                  {/if}
                </p>
              </li>
            {/each}
          </ul>
        </div>
      {/if}

      <div class="adopt-actions">
        <button
          class="btn-3d btn-3d-primary btn-3d-sm"
          onclick={handleAdopt}
        >
          Adopt (recommended)
        </button>
        <button
          class="btn-3d btn-3d-ghost btn-3d-sm"
          onclick={handleCancel}
        >
          Cancel
        </button>
        <button
          class="btn-3d btn-3d-ghost btn-3d-sm"
          onclick={() => (showDetails = !showDetails)}
        >
          {showDetails ? 'Hide details' : 'Show details'}
        </button>
      </div>
    </div>
  {/snippet}
</DialogRoot>

<style>
  .adopt-title {
    font-size: 16px;
    margin: 0 0 12px;
    color: #fff;
  }
  .adopt-modal {
    padding: 16px;
    max-width: 640px;
    color: #ccc;
  }
  .adopt-path {
    font-size: 12px;
    color: #888;
    margin: 0 0 8px;
  }
  .adopt-path code {
    background: rgba(255, 255, 255, 0.06);
    padding: 2px 6px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 11px;
  }
  .adopt-signals {
    list-style: disc;
    padding-left: 24px;
    margin: 0 0 16px;
    font-size: 13px;
  }
  .adopt-signals li {
    margin: 4px 0;
    color: #ddd;
  }
  .adopt-question {
    margin: 12px 0 8px;
    font-size: 14px;
  }
  .adopt-explanation {
    font-size: 12px;
    color: #999;
    margin: 0 0 16px;
    line-height: 1.5;
  }
  .adopt-explanation code {
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
  }
  .adopt-details {
    background: rgba(0, 0, 0, 0.2);
    padding: 12px 16px;
    border-radius: 6px;
    margin: 8px 0 16px;
    font-size: 12px;
  }
  .adopt-details h4 {
    margin: 0 0 8px;
    font-size: 12px;
    color: #aaa;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .adopt-details ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .adopt-details li {
    margin: 8px 0;
  }
  .adopt-details code {
    font-family: ui-monospace, monospace;
    font-size: 11px;
    color: #c4b3ff;
  }
  .adopt-detail-text {
    margin: 4px 0 0 0;
    color: #888;
    font-size: 11px;
    line-height: 1.5;
  }
  .adopt-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 16px;
  }
</style>
