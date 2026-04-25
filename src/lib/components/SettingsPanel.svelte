<script lang="ts">
  import { auth, currentUser } from '$lib/stores/auth';
  import { settings } from '$lib/stores/settings';

  let { open = $bindable(false) }: { open: boolean } = $props();

  let activeSection = $state<'profile' | 'downloads' | 'about'>('profile');

  // Profile edit state
  let editName = $state($currentUser?.name ?? '');
  let profileSaved = $state(false);

  currentUser.subscribe((u) => {
    if (u) editName = u.name;
  });

  async function saveProfile() {
    if (!editName.trim()) return;
    await auth.updateProfile(editName.trim());
    profileSaved = true;
    setTimeout(() => { profileSaved = false; }, 2000);
  }

  // Settings
  let installPath = $state('');
  let autoUpdate = $state(true);
  let launchOnStartup = $state(false);

  settings.subscribe((s) => {
    installPath = s.installPath;
    autoUpdate = s.autoUpdate;
    launchOnStartup = s.launchOnStartup;
  });

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') open = false;
  }

  const sections = [
    { id: 'profile' as const, label: 'Profile' },
    { id: 'downloads' as const, label: 'Downloads' },
    { id: 'about' as const, label: 'About' },
  ];
</script>

<svelte:window onkeydown={open ? handleKeydown : undefined} />

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-backdrop" onclick={() => (open = false)} onkeydown={() => {}}>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="settings-panel" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
      <!-- Left nav -->
      <div class="settings-nav">
        <h2 class="settings-title">Settings</h2>
        {#each sections as section}
          <button
            class="settings-nav-item"
            class:active={activeSection === section.id}
            onclick={() => (activeSection = section.id)}
          >
            {section.label}
          </button>
        {/each}
      </div>

      <!-- Right content -->
      <div class="settings-content">
        <button class="settings-close" onclick={() => (open = false)}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M18 6L6 18"/><path d="M6 6l12 12"/>
          </svg>
        </button>

        {#if activeSection === 'profile'}
          <h3 class="section-title">Profile</h3>
          <div class="form-group">
            <label for="settings-name">Display Name</label>
            <input
              id="settings-name"
              type="text"
              bind:value={editName}
              class="form-input"
            />
          </div>
          <div class="form-group">
            <label>Email</label>
            <input
              type="email"
              value={$currentUser?.email ?? ''}
              class="form-input"
              disabled
            />
            <p class="form-hint">Email cannot be changed here</p>
          </div>
          <div class="form-actions">
            <button class="btn-3d btn-3d-primary" onclick={saveProfile}>
              Save Changes
            </button>
            {#if profileSaved}
              <span class="save-msg">Saved!</span>
            {/if}
          </div>

        {:else if activeSection === 'downloads'}
          <h3 class="section-title">Downloads</h3>
          <div class="form-group">
            <label for="install-path">Install Location</label>
            <input
              id="install-path"
              type="text"
              bind:value={installPath}
              class="form-input mono"
              onchange={() => settings.updateSetting('installPath', installPath)}
            />
            <p class="form-hint">Where apps will be downloaded and installed</p>
          </div>
          <div class="form-group">
            <label class="toggle-row">
              <input
                type="checkbox"
                bind:checked={autoUpdate}
                onchange={() => settings.updateSetting('autoUpdate', autoUpdate)}
              />
              <span class="toggle-label">Auto-update apps</span>
            </label>
            <p class="form-hint">Automatically download and install app updates</p>
          </div>
          <div class="form-group">
            <label class="toggle-row">
              <input
                type="checkbox"
                bind:checked={launchOnStartup}
                onchange={() => settings.updateSetting('launchOnStartup', launchOnStartup)}
              />
              <span class="toggle-label">Launch on system startup</span>
            </label>
          </div>

        {:else if activeSection === 'about'}
          <h3 class="section-title">About</h3>
          <div class="about-info">
            <div class="about-logo">
              <div class="about-logo-icon">
                <span>V</span>
              </div>
              <div>
                <p class="about-name">VCT Launcher</p>
                <p class="about-version">v0.1.0</p>
              </div>
            </div>
            <div class="about-rows">
              <div class="about-row">
                <span class="about-label">Framework</span>
                <span class="about-value">Tauri 2 + SvelteKit</span>
              </div>
              <div class="about-row">
                <span class="about-label">Website</span>
                <span class="about-value about-link">vibecodedtools.com</span>
              </div>
              <div class="about-row">
                <span class="about-label">License</span>
                <span class="about-value">MIT</span>
              </div>
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

  .settings-panel {
    width: 640px;
    max-width: 90vw;
    height: 480px;
    max-height: 80vh;
    display: flex;
    background: rgba(13, 23, 53, 0.97);
    backdrop-filter: blur(24px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 20px;
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6);
    overflow: hidden;
    animation: modal-enter 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  }

  @keyframes modal-enter {
    from { opacity: 0; transform: scale(0.95) translateY(10px); }
    to { opacity: 1; transform: scale(1) translateY(0); }
  }

  .settings-nav {
    width: 180px;
    flex-shrink: 0;
    padding: 20px 12px;
    border-right: 1px solid rgba(255, 255, 255, 0.06);
    background: rgba(0, 0, 0, 0.15);
  }

  .settings-title {
    font-size: 14px;
    font-weight: 800;
    color: var(--color-text);
    padding: 0 10px;
    margin-bottom: 16px;
  }

  .settings-nav-item {
    display: block;
    width: 100%;
    padding: 8px 10px;
    font-size: 13px;
    font-weight: 500;
    color: var(--color-mid);
    background: none;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    transition: all 0.15s ease;
    margin-bottom: 2px;
  }

  .settings-nav-item:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.04);
  }

  .settings-nav-item.active {
    color: var(--color-text);
    background: rgba(0, 191, 166, 0.1);
  }

  .settings-content {
    flex: 1;
    padding: 24px;
    overflow-y: auto;
    position: relative;
  }

  .settings-close {
    position: absolute;
    top: 16px;
    right: 16px;
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

  .settings-close:hover {
    color: var(--color-text);
    background: rgba(255, 255, 255, 0.06);
  }

  .section-title {
    font-size: 18px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 24px;
  }

  .form-group {
    margin-bottom: 20px;
  }

  .form-group label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--color-mid);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .form-input {
    width: 100%;
    padding: 10px 14px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    color: var(--color-text);
    font-size: 13px;
    font-family: inherit;
    outline: none;
    transition: all 0.2s ease;
  }

  .form-input.mono {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 12px;
  }

  .form-input:focus {
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 3px rgba(0, 191, 166, 0.1);
  }

  .form-input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .form-hint {
    font-size: 11px;
    color: var(--color-muted);
    margin-top: 4px;
  }

  .form-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .save-msg {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-teal);
    animation: fade-in 0.2s ease-out;
  }

  .toggle-row {
    display: flex;
    align-items: center;
    gap: 10px;
    cursor: pointer;
    text-transform: none !important;
    letter-spacing: 0 !important;
  }

  .toggle-row input[type='checkbox'] {
    width: 18px;
    height: 18px;
    accent-color: var(--color-teal);
    cursor: pointer;
  }

  .toggle-label {
    font-size: 13px;
    font-weight: 500;
    color: var(--color-text);
  }

  /* About */
  .about-info {
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  .about-logo {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .about-logo-icon {
    width: 48px;
    height: 48px;
    border-radius: 14px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, var(--color-teal), var(--color-purple));
    box-shadow: 0 4px 16px rgba(0, 191, 166, 0.25);
  }

  .about-logo-icon span {
    color: var(--color-bg);
    font-weight: 900;
    font-size: 20px;
  }

  .about-name {
    font-size: 16px;
    font-weight: 700;
    color: var(--color-text);
  }

  .about-version {
    font-size: 12px;
    color: var(--color-mid);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .about-rows {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .about-row {
    display: flex;
    justify-content: space-between;
    padding: 8px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }

  .about-label {
    font-size: 13px;
    color: var(--color-mid);
  }

  .about-value {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-text);
  }

  .about-link {
    color: var(--color-teal);
  }
</style>
