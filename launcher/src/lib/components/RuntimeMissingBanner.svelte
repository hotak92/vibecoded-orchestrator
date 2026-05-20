<script lang="ts">
  // v0.2.22 — Item #12. Non-modal banner shown on the home page and on
  // the global Services page when neither Podman nor Docker is detected
  // on the user's machine.
  //
  // Distinct from `NoContainerRuntimeDialog`: that one is a blocking
  // modal triggered by the boot-time `vct-no-container-runtime` event
  // (auto-start path). This banner is a first-class informational
  // surface — non-blocking, always visible — so a user who dismissed
  // the modal (or whose modal was suppressed because they ran the
  // launcher with services already disabled) still sees the install
  // affordance every time they land on home / services.
  //
  // Source of truth: `orchestrator.system.has_podman` /
  // `orchestrator.system.has_docker` (populated by `detect_system` —
  // the same probe the install wizard uses). On mount we kick a
  // detectSystem() call if the store hasn't been populated yet,
  // matching the wizard's pattern.
  //
  // "Re-detect runtime" calls the existing `runtime_recheck` Tauri
  // command (which invalidates the cached runtime probe inside
  // `services::runtime`) AND then re-runs `detect_system` so the
  // store's `has_podman` / `has_docker` flags refresh too (the two
  // probes are independent and we need both to flip to dismiss the
  // banner). If either runtime is found, the banner unmounts itself
  // via the reactive `$derived` check.

  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { orchestrator } from '$lib/stores/orchestrator';

  const orchState = $derived($orchestrator);
  // Visible iff system probe ran AND both runtimes are absent. While
  // the probe is still loading (`system === null`) we render nothing
  // — better to show no banner for half a second than to flash a
  // false-positive "missing runtime" banner before detection lands.
  const visible = $derived(
    orchState.system !== null
      && !orchState.system.has_podman
      && !orchState.system.has_docker
  );

  let rechecking = $state(false);
  let recheckError = $state<string | null>(null);
  // Set to a user-facing line after a re-detect that came up empty.
  // Cleared on the next attempt. Separate from `recheckError` because
  // "still not detected" is informational, not an error.
  let recheckHint = $state<string | null>(null);

  // OS-aware "primary" link target. Podman's landing page covers Linux
  // / macOS / Windows already (the page itself routes by OS), so we
  // link to the canonical install URL and let it handle the OS fork.
  // Docker Desktop's landing page does the same. No need for a
  // per-OS branch on the Svelte side.
  const PODMAN_URL = 'https://podman.io/getting-started/installation';
  const DOCKER_URL = 'https://www.docker.com/products/docker-desktop';

  onMount(() => {
    // The orchestrator store may already have a system probe (the home
    // page's onMount calls checkStatus() which doesn't populate system,
    // but +layout.svelte's startup path or the OnboardingWizard might
    // have). If `system` is still null at mount we trigger detectSystem
    // ourselves; subsequent visits hit the cached state.
    if (orchState.system === null) {
      void orchestrator.detectSystem();
    }
  });

  async function openUrl(url: string) {
    // Prefer the Tauri-side allowlisted opener if available — it's the
    // same helper `NoContainerRuntimeDialog` uses. Falls back to
    // `window.open` in browser-preview mode so the link still works
    // during `vite dev`.
    try {
      await invoke('runtime_open_install_url', { url });
    } catch (_e) {
      window.open(url, '_blank', 'noopener,noreferrer');
    }
  }

  async function recheck() {
    rechecking = true;
    recheckError = null;
    recheckHint = null;
    try {
      // 1. Invalidate the Rust-side runtime cache + re-probe. Returns
      //    the runtime name (Some("podman") / Some("docker")) or null
      //    when neither is detected.
      const runtime = await invoke<string | null>('runtime_recheck');
      // 2. Re-run the system-level probe so `has_podman` / `has_docker`
      //    refresh in the store. `runtime_recheck` and `detect_system`
      //    are independent probes — both must agree before the banner
      //    will hide itself.
      await orchestrator.detectSystem();
      if (!runtime) {
        recheckHint = 'Still no runtime detected. If you just installed Podman/Docker, you may need to restart the launcher (or open a new shell so PATH refreshes).';
      }
    } catch (e) {
      recheckError = `Re-detect failed: ${e instanceof Error ? e.message : String(e)}`;
    } finally {
      rechecking = false;
    }
  }
</script>

{#if visible}
  <div class="rt-banner" role="status" aria-live="polite">
    <div class="rt-banner-head">
      <span class="rt-dot" aria-hidden="true"></span>
      <div class="rt-banner-copy">
        <p class="rt-lead">
          VibeCoded Tools needs Podman OR Docker to run its services
          (Weaviate, Ollama, code-embed). Neither was detected on your
          machine.
        </p>
        <p class="rt-pick">
          You only need <strong>ONE</strong> of these — pick whichever fits your workflow.
        </p>
      </div>
    </div>

    <div class="rt-cards">
      <article
        class="rt-card"
        title="Open-source container engine, rootless by default. Lightest path on Linux; on macOS / Windows it runs in a small VM (podman machine)."
      >
        <header class="rt-card-head">
          <h3>Podman</h3>
          <span class="rt-badge rt-badge-rec">Recommended</span>
        </header>
        <p class="rt-card-desc">
          Recommended for most users. Open-source, rootless by default.
        </p>
        <button
          type="button"
          class="rt-card-cta"
          onclick={() => openUrl(PODMAN_URL)}
          title="Opens podman.io in your default browser"
        >
          Download Podman →
        </button>
      </article>

      <article
        class="rt-card"
        title="Mature container ecosystem; free for personal use (commercial licensing applies for large orgs — see Docker's terms)."
      >
        <header class="rt-card-head">
          <h3>Docker Desktop</h3>
        </header>
        <p class="rt-card-desc">
          More mature ecosystem. Free for personal use; some commercial
          licensing applies.
        </p>
        <button
          type="button"
          class="rt-card-cta"
          onclick={() => openUrl(DOCKER_URL)}
          title="Opens docker.com in your default browser"
        >
          Download Docker Desktop →
        </button>
      </article>
    </div>

    <div class="rt-redetect">
      <span class="rt-redetect-text">
        Already have one installed? Click Re-detect runtime — a fresh
        install sometimes needs a launcher restart to be picked up.
      </span>
      <button
        type="button"
        class="rt-redetect-btn"
        onclick={recheck}
        disabled={rechecking}
        title="Invalidate the runtime cache and re-probe podman / docker"
      >
        {rechecking ? 'Re-detecting…' : 'Re-detect runtime'}
      </button>
    </div>

    {#if recheckHint}
      <p class="rt-hint">{recheckHint}</p>
    {/if}
    {#if recheckError}
      <p class="rt-error">{recheckError}</p>
    {/if}
  </div>
{/if}

<style>
  /* Style intent: matches the existing BrowserModeBanner / LauncherRestartBanner
     visual language — full-width amber band at the top of the content area,
     not a modal. Cards inside use the launcher's neutral surface tones
     (consistent with NoContainerRuntimeDialog's card styling). */
  .rt-banner {
    padding: 14px 20px;
    background: rgba(255, 159, 64, 0.08);
    border-bottom: 1px solid rgba(255, 159, 64, 0.30);
    color: var(--color-light, #e8e8ee);
    font-size: 13px;
    line-height: 1.5;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .rt-banner-head {
    display: flex;
    align-items: flex-start;
    gap: 12px;
  }

  .rt-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #ffb066;
    flex-shrink: 0;
    margin-top: 5px;
    box-shadow: 0 0 8px rgba(255, 176, 102, 0.6);
  }

  .rt-banner-copy {
    flex: 1;
    min-width: 0;
  }

  .rt-lead {
    margin: 0 0 4px 0;
    color: #ffd0a8;
    font-weight: 600;
  }

  .rt-pick {
    margin: 0;
    color: var(--color-mid, #b8b8c4);
    font-size: 12px;
  }

  .rt-pick strong {
    color: #ffd0a8;
    font-weight: 800;
    letter-spacing: 0.02em;
  }

  .rt-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }

  .rt-card {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .rt-card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .rt-card h3 {
    margin: 0;
    font-size: 14px;
    font-weight: 700;
    color: var(--color-text, #f0f0f0);
  }

  .rt-badge {
    font-size: 10px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .rt-badge-rec {
    color: var(--color-teal, #00bfa6);
    background: rgba(0, 191, 166, 0.12);
    border: 1px solid rgba(0, 191, 166, 0.30);
  }

  .rt-card-desc {
    margin: 0;
    font-size: 12px;
    color: var(--color-mid, #b8b8c4);
    line-height: 1.5;
    flex: 1;
  }

  .rt-card-cta {
    align-self: flex-start;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: inherit;
    padding: 5px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    font-family: inherit;
  }

  .rt-card-cta:hover {
    background: rgba(0, 191, 166, 0.10);
    border-color: rgba(0, 191, 166, 0.40);
    color: var(--color-teal, #00bfa6);
  }

  .rt-card-cta:focus-visible {
    outline: 2px solid var(--color-teal, #00bfa6);
    outline-offset: 2px;
  }

  .rt-redetect {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    padding-top: 6px;
    border-top: 1px dashed rgba(255, 159, 64, 0.20);
  }

  .rt-redetect-text {
    color: var(--color-mid, #b8b8c4);
    font-size: 12px;
    flex: 1;
    min-width: 220px;
  }

  .rt-redetect-btn {
    background: rgba(255, 159, 64, 0.16);
    border: 1px solid rgba(255, 159, 64, 0.40);
    color: #ffd0a8;
    padding: 5px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    font-family: inherit;
    flex-shrink: 0;
  }

  .rt-redetect-btn:hover:not(:disabled) {
    background: rgba(255, 159, 64, 0.24);
  }

  .rt-redetect-btn:focus-visible {
    outline: 2px solid #ffb066;
    outline-offset: 2px;
  }

  .rt-redetect-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .rt-hint {
    margin: 0;
    font-size: 12px;
    color: var(--color-mid, #b8b8c4);
    font-style: italic;
  }

  .rt-error {
    margin: 0;
    font-size: 12px;
    color: #ff6b6b;
  }
</style>
