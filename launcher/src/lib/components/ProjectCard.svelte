<script lang="ts">
  // v0.2.49 access-matrix overhaul, Phase 6 S-4 (Stream W3).
  //
  // Single project card used by /projects (the project list) and any other
  // surface that needs to render a clickable project tile.
  //
  // Carries the v0.2.49 folder-missing warning banner: when the launcher's
  // boot sanity check (lib.rs setup async spawn) stamped
  // projects.folder_missing_at_last_boot = true for this project, the
  // card surfaces a non-blocking pink-accented banner with the folder
  // path and a hint that the user should check whether they moved or
  // deleted the folder. The banner is purely informational — it does
  // NOT gate clicks; the project remains openable so users can still
  // get to its Settings tab to repoint or unregister.
  //
  // Brand: card uses the standard launcher card chrome (rgba white-on-
  // navy 4% fill, 8% border). Active state and warning banner both
  // pick up the brand colors (teal #00BFA6 for active, pink #FF4FA0
  // for warning) via inline rgba() literals matching app.css tokens.

  interface Props {
    /** Project view returned by list_projects_v2. */
    project: {
      id: string;
      name: string;
      folder_path: string;
      host?: string;
    };
    /** True when this card represents the currently-selected project. */
    active?: boolean;
    /**
     * v0.2.49 Phase 6 S-4 — boot probe verdict for this project. When
     * true, the card renders the warning banner ("Folder not found at
     * <path>. Did you move or delete it?"). The flag is set by the
     * boot probe (see `commands::project_folder_health::run_folder_probe`
     * in the launcher's setup path) and cleared on the next boot if
     * the folder reappears. Default false so existing call sites that
     * don't pass it still render correctly.
     */
    folderMissing?: boolean;
    /** Click handler — receives the project id. */
    onOpen: (id: string) => void;
  }

  let { project, active = false, folderMissing = false, onOpen }: Props = $props();

  function handleKey(e: KeyboardEvent) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen(project.id);
    }
  }
</script>

<div
  class="pc-card"
  class:active
  class:warn={folderMissing}
  role="button"
  tabindex="0"
  onclick={() => onOpen(project.id)}
  onkeydown={handleKey}
>
  <header class="pc-head">
    <h3>{project.name}</h3>
    {#if active}
      <span class="pc-badge">ACTIVE</span>
    {/if}
  </header>
  <p class="pc-path"><code>{project.folder_path}</code></p>
  <p class="pc-meta">
    <span>{(project.host ?? 'BASE').toUpperCase()}</span>
  </p>

  {#if folderMissing}
    <!-- v0.2.49 Phase 6 S-4 boot probe banner. Non-blocking; the card
         is still clickable so users can get to Settings → Repoint /
         Unregister even when the folder is gone. -->
    <div class="pc-warn-banner" role="alert" data-testid="folder-missing-banner">
      <span class="pc-warn-icon" aria-hidden="true">⚠</span>
      <span class="pc-warn-copy">
        Folder not found at <code>{project.folder_path}</code>.
        Did you move or delete it?
      </span>
    </div>
  {/if}
</div>

<style>
  .pc-card {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 14px 16px;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .pc-card:hover {
    background: rgba(255, 255, 255, 0.08);
    border-color: rgba(255, 255, 255, 0.15);
  }
  .pc-card.active {
    border-color: rgba(0, 191, 166, 0.4);
    background: rgba(0, 191, 166, 0.05);
  }
  /* v0.2.49 Phase 6 S-4: warning chrome when the boot probe flagged
     this project's folder as missing. Pink token (#FF4FA0 from the
     brand reference) signals "needs attention" without competing with
     the active-card teal. Subtle border tint + faint fill — the
     banner itself carries the explicit copy. */
  .pc-card.warn {
    border-color: rgba(255, 79, 160, 0.5);
    background: rgba(255, 79, 160, 0.04);
  }
  .pc-card.warn:hover {
    background: rgba(255, 79, 160, 0.08);
    border-color: rgba(255, 79, 160, 0.65);
  }
  .pc-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .pc-head h3 {
    margin: 0;
    font-size: 15px;
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pc-badge {
    background: rgba(0, 191, 166, 0.15);
    color: rgb(0, 191, 166);
    border: 1px solid rgba(0, 191, 166, 0.3);
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }
  .pc-path {
    margin: 0;
    font-size: 11px;
    color: #888;
    word-break: break-all;
  }
  .pc-path code {
    background: rgba(255, 255, 255, 0.05);
    padding: 1px 5px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
  }
  .pc-meta {
    margin: 0;
    font-size: 11px;
    color: #aaa;
  }
  .pc-meta span {
    background: rgba(255, 255, 255, 0.05);
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
  }
  .pc-warn-banner {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    margin-top: 4px;
    padding: 8px 10px;
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.3);
    border-radius: 6px;
    font-size: 11px;
    color: rgba(255, 79, 160, 0.95);
    line-height: 1.4;
  }
  .pc-warn-icon {
    flex-shrink: 0;
    font-size: 13px;
    line-height: 1.2;
  }
  .pc-warn-copy code {
    background: rgba(255, 79, 160, 0.12);
    padding: 1px 4px;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    color: #ffb4d4;
    word-break: break-all;
  }
</style>
