<script lang="ts">
  import { licenses, type License } from '$lib/stores/licenses';

  let { open = $bindable(false) }: { open: boolean } = $props();

  let code = $state('');
  let validating = $state(false);
  let error = $state<string | null>(null);
  let success = $state<string | null>(null);

  // Subscribe to license store for messages
  licenses.subscribe((s) => {
    validating = s.validating;
    error = s.error;
    success = s.success;
  });

  async function handleActivate() {
    if (!code.trim()) return;
    const ok = await licenses.validateCode(code.trim());
    if (ok) {
      code = '';
    }
  }

  function handleClose() {
    open = false;
    licenses.clearMessages();
    code = '';
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') handleClose();
  }

  let licenseList = $state<License[]>([]);
  licenses.subscribe((s) => { licenseList = s.licenses; });
</script>

<svelte:window onkeydown={open ? handleKeydown : undefined} />

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-backdrop" onclick={handleClose} onkeydown={() => {}}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal-content" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
      <div class="modal-header">
        <h2>Activation Codes</h2>
        <button class="modal-close" onclick={handleClose}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 6L6 18"/><path d="M6 6l12 12"/>
          </svg>
        </button>
      </div>

      <div class="modal-body">
        <p class="modal-desc">Enter your activation code to unlock a tool.</p>

        <div class="activate-form">
          <input
            type="text"
            bind:value={code}
            placeholder="XXXX-XXXX-XXXX-XXXX"
            class="activate-input"
            onkeydown={(e) => { if (e.key === 'Enter') handleActivate(); }}
          />
          <button
            class="btn-3d btn-3d-primary"
            onclick={handleActivate}
            disabled={validating || !code.trim()}
          >
            {#if validating}
              <span class="spinner"></span>
            {:else}
              Activate
            {/if}
          </button>
        </div>

        {#if error}
          <div class="msg msg-error">{error}</div>
        {/if}

        {#if success}
          <div class="msg msg-success">{success}</div>
        {/if}

        {#if licenseList.length > 0}
          <div class="licenses-section">
            <h3>Active Licenses</h3>
            <div class="license-list">
              {#each licenseList as license}
                <div class="license-row">
                  <div class="license-info">
                    <span class="license-app">{license.appName}</span>
                    <span class="license-key">{license.key}</span>
                  </div>
                  <span class="license-status">{license.status}</span>
                </div>
              {/each}
            </div>
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 300;
    animation: fade-in 0.15s ease-out;
  }

  @keyframes fade-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .modal-content {
    width: 480px;
    max-width: 90vw;
    max-height: 80vh;
    overflow-y: auto;
    background: rgba(13, 23, 53, 0.97);
    backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 20px;
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6);
    animation: modal-enter 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  }

  @keyframes modal-enter {
    from { opacity: 0; transform: scale(0.95) translateY(10px); }
    to { opacity: 1; transform: scale(1) translateY(0); }
  }

  .modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 20px 24px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }

  .modal-header h2 {
    font-size: 16px;
    font-weight: 700;
    color: var(--color-text);
  }

  .modal-close {
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid transparent;
    border-radius: 8px;
    color: var(--color-mid);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .modal-close:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
    border-color: rgba(255, 255, 255, 0.08);
  }

  .modal-body {
    padding: 24px;
  }

  .modal-desc {
    font-size: 13px;
    color: var(--color-mid);
    margin-bottom: 18px;
  }

  .activate-form {
    display: flex;
    gap: 10px;
  }

  .activate-input {
    flex: 1;
    padding: 10px 16px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 12px;
    color: var(--color-text);
    font-size: 14px;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    letter-spacing: 1px;
    outline: none;
    transition: border-color 0.2s ease;
  }

  .activate-input::placeholder {
    color: var(--color-muted);
    letter-spacing: 2px;
  }

  .activate-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 3px rgba(0, 191, 166, 0.1);
  }

  .msg {
    margin-top: 14px;
    padding: 10px 14px;
    border-radius: 10px;
    font-size: 13px;
  }

  .msg-error {
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    color: var(--color-pink);
  }

  .msg-success {
    background: rgba(0, 191, 166, 0.1);
    border: 1px solid rgba(0, 191, 166, 0.25);
    color: var(--color-teal);
  }

  .licenses-section {
    margin-top: 24px;
    padding-top: 20px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }

  .licenses-section h3 {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--color-muted);
    margin-bottom: 12px;
  }

  .license-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .license-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 10px;
  }

  .license-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .license-app {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-text);
  }

  .license-key {
    font-size: 11px;
    color: var(--color-muted);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .license-status {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-teal);
    background: rgba(0, 191, 166, 0.1);
    padding: 2px 10px;
    border-radius: 20px;
    text-transform: capitalize;
  }

  .spinner {
    display: inline-block;
    width: 16px;
    height: 16px;
    border: 2px solid rgba(5, 11, 31, 0.3);
    border-top-color: var(--color-bg);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
