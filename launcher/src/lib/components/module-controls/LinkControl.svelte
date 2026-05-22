<script lang="ts">
  // LinkControl — clickable hyperlink.
  //
  // v0.2.26 control kind: `link`.
  //
  //   * `target: "external"` (default) → opens the URL in the system
  //     browser via `@tauri-apps/plugin-opener::openUrl`.
  //   * `target: "internal"`           → SvelteKit `goto(href)` navigation
  //     inside the launcher.
  //
  // Visual style: inline hyperlink. The user explicitly said they want
  // this to feel like a normal link, not a button — so no button chrome.
  // We render an actual <a> element so right-click "Copy link",
  // middle-click, and keyboard activation all work the way users expect.

  import { goto } from '$app/navigation';
  import { isTauriRuntime } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { LinkControl } from '$lib/types/manifest';

  let {
    control,
    disabled = false,
  }: {
    control: LinkControl;
    disabled?: boolean;
  } = $props();

  const isInternal = $derived(control.target === 'internal');

  async function activate(e: MouseEvent | KeyboardEvent) {
    if (disabled) {
      e.preventDefault();
      return;
    }
    // For internal navigation, always intercept (we use SvelteKit goto,
    // not browser navigation, so the SPA state is preserved).
    if (isInternal) {
      e.preventDefault();
      try {
        await goto(control.href);
      } catch (err) {
        toast.error(
          `Could not navigate to ${control.href}: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
      return;
    }

    // External: prefer the Tauri plugin-opener so we don't fight the
    // webview's default navigation behaviour (which in Tauri may load
    // the URL inside the app window — not what users want).
    if (isTauriRuntime()) {
      e.preventDefault();
      try {
        const { openUrl } = await import('@tauri-apps/plugin-opener');
        await openUrl(control.href);
      } catch (err) {
        toast.error(
          `Could not open ${control.href}: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
      return;
    }

    // Browser mode (vite dev / preview): let the anchor's
    // target=_blank behaviour handle it. Don't preventDefault.
  }

  function onKeydown(e: KeyboardEvent) {
    // Anchors already activate on Enter, but only Enter — keep parity
    // with the rest of the toolkit by also accepting Space.
    if (e.key === ' ') {
      e.preventDefault();
      void activate(e);
    }
  }
</script>

<div class="link-control">
  <a
    class="link"
    class:disabled
    href={control.href}
    target={isInternal ? undefined : '_blank'}
    rel={isInternal ? undefined : 'noopener noreferrer'}
    onclick={activate}
    onkeydown={onKeydown}
    aria-disabled={disabled}
    title={control.tooltip ?? control.label}
  >
    <span class="link-label">{control.label}</span>
    {#if !isInternal}
      <span class="external-icon" aria-hidden="true">↗</span>
    {/if}
  </a>
  {#if control.tooltip}
    <span
      class="tooltip-affordance"
      title={control.tooltip}
      aria-label="More info"
    >?</span>
  {/if}
</div>

<style>
  .link-control {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .link {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: #00bfa6;
    text-decoration: underline;
    text-underline-offset: 2px;
    font-size: 13px;
    cursor: pointer;
    border-radius: 3px;
  }
  .link:hover {
    color: #1ad3ba;
  }
  .link:focus-visible {
    outline: 2px solid rgba(0, 191, 166, 0.55);
    outline-offset: 2px;
  }
  .link.disabled {
    color: var(--color-muted);
    pointer-events: none;
    text-decoration: none;
  }

  .external-icon {
    font-size: 10px;
    opacity: 0.7;
  }

  .tooltip-affordance {
    display: inline-flex;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    align-items: center;
    justify-content: center;
    background: rgba(255, 255, 255, 0.08);
    color: var(--color-muted);
    font-size: 10px;
    font-weight: 700;
    cursor: help;
    flex-shrink: 0;
  }
  .tooltip-affordance:hover {
    background: rgba(255, 255, 255, 0.16);
    color: var(--color-text);
  }
</style>
