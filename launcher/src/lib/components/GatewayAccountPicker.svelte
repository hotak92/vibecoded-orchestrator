<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  // v0.2.105 — Project → Identity → "Claude account": which of this user's
  // Claude logins the model gateway serves this project with. Hidden while
  // only the primary login exists. The choice is written by the env
  // projection into the project's .claude/settings.local.json
  // (ANTHROPIC_CUSTOM_HEADERS), and the next request of a running chat
  // already uses it — no restart.
  import { onMount } from 'svelte';
  import { toast } from '$lib/stores/toast';
  import {
    listAccounts,
    pickerVisible,
    projectAccountStatus,
    projectVerdict,
    setProjectAccount,
    type AccountsListing,
    type ProjectAccountStatus,
  } from '$lib/api/gateway_accounts';

  let { projectId }: { projectId: string } = $props();

  let listing = $state<AccountsListing | null>(null);
  let status = $state<ProjectAccountStatus | null>(null);
  let choice = $state<string>('');
  let busy = $state(false);
  let error = $state<string | null>(null);

  const verdict = $derived(status ? projectVerdict(status) : null);
  const defaultLabel = $derived(
    listing?.accounts.find((a) => a.is_default)?.label ?? 'primary',
  );

  async function loadAccountChoice() {
    error = null;
    try {
      listing = await listAccounts();
      if (!pickerVisible(listing)) return;
      status = await projectAccountStatus(projectId);
      choice = status.assigned ?? '';
    } catch (e) {
      error = String(e);
    }
  }

  async function applyAccountChoice(next: string) {
    busy = true;
    try {
      const answer = await setProjectAccount(projectId, next === '' ? null : next);
      if (!answer.ok) {
        toast.error(answer.error ?? 'The account choice was not saved.');
      } else {
        toast.success('Claude account saved — the next request of a running chat uses it.');
      }
      await loadAccountChoice();
    } catch (e) {
      toast.error(String(e));
    } finally {
      busy = false;
    }
  }

  onMount(loadAccountChoice);
</script>

{#if error}
  <div class="ps-section">
    <h4>Claude account</h4>
    <p class="ps-form-hint">Claude accounts could not be read: {error}</p>
  </div>
{:else if pickerVisible(listing) && listing}
  <div class="ps-section">
    <h4>Claude account</h4>
    <label class="ps-span2">
      <span>Account the model gateway serves this project with</span>
      <select
        bind:value={choice}
        disabled={busy}
        onchange={() => applyAccountChoice(choice)}
        aria-label="Claude account for this project"
      >
        <option value="">Use the default ({defaultLabel})</option>
        {#each listing.accounts as acct (acct.id)}
          <option value={acct.id}>{acct.label} ({acct.id})</option>
        {/each}
      </select>
    </label>
    {#if verdict}
      <p class="acct-verdict acct-{verdict.tone}">{verdict.text}</p>
    {/if}
    {#if status?.drift}
      <button class="ps-btn-secondary" disabled={busy} onclick={() => applyAccountChoice(choice)}>
        Re-apply
      </button>
    {/if}
    <small class="ps-form-hint">
      Applies to chats that go through the model gateway (the VS Code panel in multi-model mode,
      or a CLI pointed at it). Written as <code>ANTHROPIC_CUSTOM_HEADERS</code> in this project's
      <code>.claude/settings.local.json</code> (needs Claude Code {listing.min_claude_code_version}
      or later). The prompt cache does not carry across accounts. Manage accounts in
      Preferences → Claude accounts.
    </small>
  </div>
{/if}

<style>
  .acct-verdict {
    margin: 0.5rem 0;
    font-size: 0.9rem;
  }
  .acct-ok {
    color: var(--color-teal, #00bfa6);
  }
  .acct-warn {
    color: #f5b942;
  }
  .acct-bad {
    color: var(--color-pink, #ff4fa0);
  }
</style>
