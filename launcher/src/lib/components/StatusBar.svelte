<script lang="ts">
  import { onMount } from 'svelte';
  import { getVersion } from '@tauri-apps/api/app';
  import { currentUser } from '$lib/stores/auth';

  let appCount = $derived($currentUser?.apps?.length ?? 0);
  let version = $state('');

  onMount(async () => {
    try {
      version = await getVersion();
    } catch {
      // getVersion may fail in dev/web preview without Tauri runtime —
      // leave version blank rather than show a stale hardcoded number.
      version = '';
    }
  });
</script>

<footer class="status-bar">
  <div class="status-left">
    <div class="status-dot"></div>
    <span>Connected</span>
  </div>
  <div class="status-right">
    <span>{appCount} app{appCount !== 1 ? 's' : ''} activated</span>
    {#if version}
      <span class="status-sep">|</span>
      <!-- v0.2.43 (Fabio branch feat/launcher-logo-circular-white): mini
           brand watermark next to the version. Replaces the menubar logo
           (which was visually duplicating the Windows titlebar icon).
           Keeps the brand visible without stealing top-bar real estate. -->
      <span class="status-brand" aria-hidden="true">
        <img src="/logo.png" alt="" class="status-brand-logo" />
        <span>VCT</span>
      </span>
      <span>v{version}</span>
    {/if}
  </div>
</footer>

<style>
  .status-bar {
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 16px;
    background: rgba(5, 11, 31, 0.9);
    border-top: 1px solid rgba(255, 255, 255, 0.04);
    flex-shrink: 0;
    font-size: 11px;
    color: var(--color-muted);
  }

  .status-left {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--color-teal);
    box-shadow: 0 0 8px rgba(0, 191, 166, 0.6);
  }

  .status-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .status-sep {
    opacity: 0.3;
  }

  .status-brand {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--color-mid, #94A3B8);
    font-weight: 600;
    letter-spacing: 0.3px;
  }
  .status-brand-logo {
    width: 14px;
    height: 14px;
    display: block;
    user-select: none;
    -webkit-user-drag: none;
  }
</style>
