<script lang="ts">
  // Inline banner shown on routes that need a selected project (hub,
  // coordination, telemetry, etc.). Renders a one-line explanation +
  // a button that takes the user to the project picker. Replaces the
  // older "silently auto-redirect to /project" behavior the joint
  // Round 3 verdict flagged as confusing for multi-tenant users.

  import { goto } from '$app/navigation';

  let {
    section = 'this section',
    href = '/project',
  }: {
    /** Human label of the area being gated, e.g. "Coordination". */
    section?: string;
    /** Where the "Choose project" button takes the user. */
    href?: string;
  } = $props();

  function pick() {
    goto(href);
  }
</script>

<div class="np-banner" role="status">
  <div class="np-text">
    <strong>Select a project</strong>
    <span>to view {section}.</span>
  </div>
  <button class="np-btn" type="button" onclick={pick}>Choose project →</button>
</div>

<style>
  .np-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 12px 16px;
    margin: 16px 24px;
    background: rgba(0, 191, 166, 0.06);
    border: 1px solid rgba(0, 191, 166, 0.25);
    border-radius: 10px;
    color: var(--color-text, #e8e8ee);
    font-size: 13px;
  }
  .np-text {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .np-text strong {
    color: var(--color-teal);
    font-weight: 700;
  }
  .np-btn {
    flex-shrink: 0;
    padding: 6px 14px;
    background: rgba(0, 191, 166, 0.18);
    border: 1px solid rgba(0, 191, 166, 0.5);
    border-radius: 8px;
    color: var(--color-teal);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .np-btn:hover {
    background: rgba(0, 191, 166, 0.28);
    border-color: rgba(0, 191, 166, 0.7);
  }
</style>
