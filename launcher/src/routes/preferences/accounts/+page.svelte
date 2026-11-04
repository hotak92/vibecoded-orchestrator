<script lang="ts">
  /**
   * /preferences/accounts — Claude accounts the model gateway can serve
   * (v0.2.105).
   *
   * A second login (for example a seat on a second organisation account)
   * lives in its own Claude Code configuration directory. VCO stores the id,
   * a label and that directory — never a credential — and the user signs in
   * THEMSELVES with the command shown here (VCO never runs a login or opens
   * a browser). Each project then picks its account (Project → Identity →
   * Claude account); a project that picks none uses the default.
   *
   * An account that cannot be used never fails a chat: the gateway serves the
   * request with the default/primary account, writes a warning, tells the
   * model once, and this page shows the count and the reason.
   */
  import { onMount } from 'svelte';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import {
    addAccount,
    listAccounts,
    loginVerdict,
    removeAccount,
    setDefaultAccount,
    suggestedConfigDir,
    validateNewAccount,
    type AccountsListing,
    type MutationAnswer,
  } from '$lib/api/gateway_accounts';

  let listing = $state<AccountsListing | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let loadError = $state<string | null>(null);

  let newId = $state('');
  let newLabel = $state('');
  let newDir = $state('');
  let formError = $state<string | null>(null);

  async function loadAccounts() {
    loadError = null;
    try {
      listing = await listAccounts();
    } catch (e) {
      loadError = String(e);
    } finally {
      loading = false;
    }
  }

  async function mutate(run: () => Promise<MutationAnswer>, done: string) {
    busy = true;
    try {
      const answer = await run();
      if (answer.ok) toast.success(done);
      else toast.error(answer.error ?? 'The change was not saved.');
      await loadAccounts();
      return answer.ok;
    } catch (e) {
      toast.error(String(e));
      return false;
    } finally {
      busy = false;
    }
  }

  async function submitNewAccount() {
    const dir = newDir.trim() || suggestedConfigDir(newId);
    formError = validateNewAccount(newId, dir, listing?.accounts.map((a) => a.id) ?? []);
    if (formError) return;
    const ok = await mutate(
      () => addAccount(newId.trim(), newLabel.trim(), dir),
      `Account "${newId.trim()}" added. Log it in with the command shown in its row.`,
    );
    if (ok) {
      newId = '';
      newLabel = '';
      newDir = '';
    }
  }

  function copy(text: string) {
    navigator.clipboard?.writeText(text).then(
      () => toast.success('Command copied'),
      () => toast.error('Could not copy — select the text instead'),
    );
  }

  onMount(loadAccounts);
</script>

<Toast />

<div class="page">
  <nav class="crumb">
    <a href="/preferences">Preferences</a>
    <span class="sep">/</span>
    <span class="current">Claude accounts</span>
  </nav>

  <header class="hdr">
    <h1>Claude accounts</h1>
    <p class="hint">
      The model gateway can serve more than one of <em>your</em> Claude logins — for example your
      personal plan and a seat on a second organisation account. Each project picks one in
      Project → Identity → Claude account; a project that picks none uses the default. VCO keeps
      only the account's name and configuration directory: you sign in yourself with the command
      shown, and Claude Code keeps the login in that directory. The gateway renews an added
      account's login on its own shortly before it expires.
    </p>
    <p class="hint">
      If an account cannot be used (not logged in, expired, renewal failed), the chat is never
      stopped: the request is served — and billed — by the default account (else the primary
      one), the gateway log records a warning, the model is told once per conversation, and the
      row below shows it.
    </p>
  </header>

  {#if loading}
    <p class="empty">Loading…</p>
  {:else if loadError}
    <p class="warn">Accounts could not be read: {loadError}</p>
  {:else if listing}
    {#if !listing.gateway.ok}
      <p class="warn">
        The running gateway's view is not available ({listing.gateway.reason ?? 'unknown'}): login
        state below is read from the files directly.
        {#if listing.gateway.reason === 'outdated_gateway'}Restart the gateway after the update.{/if}
      </p>
    {/if}

    <section class="card">
      {#each listing.accounts as acct (acct.id)}
        {@const verdict = loginVerdict(acct)}
        <div class="acct-row">
          <div class="acct-main">
            <strong>{acct.label}</strong>
            <code>{acct.id}</code>
            {#if acct.is_default}<span class="badge">default</span>{/if}
            <span class="state state-{verdict.tone}">{verdict.text}</span>
          </div>
          <div class="acct-meta">
            <span class="dim">Configuration directory:</span> <code>{acct.config_dir}</code>
          </div>
          {#if acct.projects.length}
            <div class="acct-meta">
              <span class="dim">Selected by {acct.projects.length} project(s)</span>
            </div>
          {/if}
          {#if acct.gateway && acct.gateway.fallbacks > 0}
            <p class="warn">
              {acct.gateway.fallbacks} request(s) asked for this account and were served by another
              one (last reason: {acct.gateway.last_fallback_reason}).
            </p>
          {/if}
          {#if acct.gateway?.refresh}
            <div class="acct-meta">
              <span class="dim">Last automatic renewal:</span>
              {acct.gateway.refresh.outcome} — {acct.gateway.refresh.detail}
            </div>
          {/if}
          {#if !acct.is_primary && acct.login_commands && verdict.tone !== 'ok'}
            <div class="login">
              <span class="dim">Sign in (run it yourself in a terminal):</span>
              <code class="cmd">{acct.login_commands.posix}</code>
              <button class="btn small" onclick={() => copy(acct.login_commands!.posix)}>Copy</button>
              <span class="dim">PowerShell:</span>
              <code class="cmd">{acct.login_commands.powershell}</code>
              <button class="btn small" onclick={() => copy(acct.login_commands!.powershell)}>Copy</button>
            </div>
          {/if}
          <div class="actions">
            {#if !acct.is_default}
              <button
                class="btn"
                disabled={busy}
                onclick={() => mutate(() => setDefaultAccount(acct.id), `"${acct.id}" is now the default`)}
              >
                Make default
              </button>
            {/if}
            {#if !acct.is_primary}
              <button
                class="btn danger"
                disabled={busy || acct.is_default || acct.projects.length > 0}
                title={acct.projects.length
                  ? 'Switch the projects that select it first'
                  : acct.is_default
                    ? 'Make another account the default first'
                    : 'Forget this account (its login directory is left untouched)'}
                onclick={() => mutate(() => removeAccount(acct.id), `Account "${acct.id}" removed`)}
              >
                Remove
              </button>
            {/if}
          </div>
        </div>
      {/each}
    </section>

    <section class="card">
      <h2>Add an account</h2>
      <p class="hint">
        Every account listed here must be yours. VCO never switches between them on its own and
        never shares them; a project uses the one it selects.
      </p>
      <div class="form">
        <label>
          <span>Id</span>
          <input bind:value={newId} placeholder="work" spellcheck="false" disabled={busy} />
        </label>
        <label>
          <span>Label</span>
          <input bind:value={newLabel} placeholder="Organisation seat" disabled={busy} />
        </label>
        <label class="wide">
          <span>Configuration directory</span>
          <input
            bind:value={newDir}
            placeholder={suggestedConfigDir(newId) || '~/.claude-work'}
            spellcheck="false"
            disabled={busy}
          />
        </label>
      </div>
      {#if formError}<p class="warn">{formError}</p>{/if}
      <button class="btn" disabled={busy || !newId.trim()} onclick={submitNewAccount}>Add account</button>
    </section>
  {/if}
</div>

<style>
  .page {
    padding: 1.5rem;
    max-width: 1180px;
    margin: 0 auto;
  }
  .crumb {
    margin-bottom: 1rem;
    font-size: 0.9rem;
    color: var(--color-mid, #94a3b8);
  }
  .crumb a {
    color: var(--color-teal, #00bfa6);
    text-decoration: none;
  }
  .crumb .sep {
    margin: 0 0.4rem;
  }
  .hdr h1 {
    margin: 0 0 0.5rem;
    font-size: 1.5rem;
  }
  .hint {
    color: var(--color-mid, #94a3b8);
    font-size: 0.9rem;
    max-width: 70ch;
  }
  .card {
    background: var(--color-surface, rgba(255, 255, 255, 0.03));
    border: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
    border-radius: 12px;
    padding: 1rem 1.25rem;
    margin: 1rem 0;
  }
  .acct-row {
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--color-border, rgba(255, 255, 255, 0.08));
  }
  .acct-row:last-child {
    border-bottom: none;
  }
  .acct-main {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    flex-wrap: wrap;
  }
  .acct-meta {
    font-size: 0.85rem;
    margin-top: 0.25rem;
  }
  .badge {
    background: var(--color-purple, #7b5fff);
    color: #fff;
    border-radius: 999px;
    padding: 0 0.5rem;
    font-size: 0.75rem;
  }
  .state {
    font-size: 0.85rem;
  }
  .state-ok {
    color: var(--color-teal, #00bfa6);
  }
  .state-warn {
    color: #f5b942;
  }
  .state-bad {
    color: var(--color-pink, #ff4fa0);
  }
  .dim {
    color: var(--color-mid, #94a3b8);
  }
  .warn {
    color: #f5b942;
    font-size: 0.9rem;
  }
  .login {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 0.4rem 0.6rem;
    align-items: center;
    margin-top: 0.5rem;
    font-size: 0.85rem;
  }
  .cmd {
    overflow-x: auto;
    white-space: nowrap;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }
  .form {
    display: grid;
    grid-template-columns: 1fr 1fr 2fr;
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }
  .form label {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: 0.85rem;
  }
  .btn {
    background: var(--color-teal, #00bfa6);
    color: #0b1220;
    border: none;
    border-radius: 8px;
    padding: 0.4rem 0.9rem;
    cursor: pointer;
  }
  .btn.small {
    padding: 0.15rem 0.6rem;
  }
  .btn.danger {
    background: var(--color-pink, #ff4fa0);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .empty {
    color: var(--color-mid, #94a3b8);
  }
</style>
