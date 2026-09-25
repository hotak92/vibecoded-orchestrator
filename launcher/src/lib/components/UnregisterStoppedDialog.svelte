<script lang="ts">
  // The unregister STOP, shown as a dialog with its explicit escape (owner
  // ruling, v0.2.97 review R5 F39). The stop stays the default — the primary
  // (safe) button keeps the project registered; the second action,
  // "Unregister anyway — leave these values", finishes the unregister and has
  // VCO write a note listing each key and file to clean by hand (names only).
  // The flow itself lives in `$lib/unregister-escape` (unit-tested).

  import DialogRoot from '$lib/components/DialogRoot.svelte';
  import { UNREGISTER_ANYWAY_LABEL } from '$lib/unregister-escape';

  let {
    open = $bindable<boolean>(false),
    message,
    projectName,
    onDecide,
  }: {
    open?: boolean;
    message: string;
    projectName: string;
    onDecide: (leaveAnyway: boolean) => void;
  } = $props();

  // One decision per opening: closing the dialog (Escape, backdrop) after a
  // button already decided must not report a second, contrary answer.
  let decided = $state(false);
  $effect(() => {
    if (open) decided = false;
  });

  function decide(leaveAnyway: boolean) {
    if (decided) return;
    decided = true;
    open = false;
    onDecide(leaveAnyway);
  }
</script>

<DialogRoot
  bind:open
  onClose={() => decide(false)}
  width="560px"
  ariaLabelledBy="unregister-stopped-title"
>
  {#snippet header()}
    <h2 id="unregister-stopped-title">Unregister stopped</h2>
    <p class="stage-tag">"{projectName}" is still registered</p>
  {/snippet}
  {#snippet body()}
    <p class="stop-message">{message}</p>
    <p class="hint">
      Keeping the project lets you fix the cause and unregister again — VCO can
      still remove these values then. "Unregister anyway" finishes now and leaves
      them on disk; a note lists each key and file (never a value).
    </p>
  {/snippet}
  {#snippet footer()}
    <div class="actions">
      <button type="button" class="btn-3d btn-3d-accent btn-3d-sm" onclick={() => decide(true)}>
        {UNREGISTER_ANYWAY_LABEL}
      </button>
      <button type="button" class="btn-3d btn-3d-primary btn-3d-sm" onclick={() => decide(false)}>
        Keep the project
      </button>
    </div>
  {/snippet}
</DialogRoot>

<style>
  h2 {
    margin: 0;
    font-size: 18px;
    font-weight: 700;
  }
  .stage-tag {
    margin: 0.35rem 0 0;
    font-size: 11px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--color-teal, #00bfa6);
  }
  .stop-message {
    margin: 0 0 1rem;
    padding: 0.85rem 1rem;
    border-radius: 12px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 79, 160, 0.3);
    font-size: 0.85rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 40vh;
    overflow-y: auto;
  }
  .hint {
    margin: 0;
    font-size: 0.8rem;
    font-style: italic;
    color: var(--text-color-muted, #a0a0a0);
  }
  .actions {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
    flex-wrap: wrap;
  }
</style>
