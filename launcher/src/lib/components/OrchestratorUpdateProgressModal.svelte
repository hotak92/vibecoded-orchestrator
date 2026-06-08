<script lang="ts">
  // SPDX-License-Identifier: AGPL-3.0-or-later
  //
  // OrchestratorUpdateProgressModal — full-screen blocking overlay for
  // the orchestrator self-update flow (contributor branch, v0.2.43-targeting).
  //
  // Why this exists:
  //
  //   Until this branch landed, clicking Update / Install Update / Restart
  //   Launcher in `UpdateBadge.svelte` closed the popover and left the UI
  //   silent for 5–15 minutes while `update_orchestrator` / `apply_pending_install`
  //   / `restart_launcher` ran in the Rust backend. The only signal was a
  //   ~14px spinning icon in the menubar. Meanwhile the vct-hub was stopped,
  //   the launcher binary was renamed on disk (Windows pre-pull rename),
  //   and `install.py --update` was mutating `.claude/`. The user could
  //   click into other tabs, open projects, edit KG — all of which fail or
  //   race the updater. The fix is a blocking overlay that lasts the entire
  //   update lifetime, plus the existing popover-close on click.
  //
  // Architecture (per A3 audit + Martino's wiring guidance, 30/05/2026):
  //
  //   - Visibility gated by `$ui.showOrchestratorUpdateProgress` (a flag
  //     in `stores/ui.ts` reserved by the v0.2.40 L1 collision audit so
  //     this branch doesn't fight `showLicenseManager` over namespace).
  //   - Mounted in `+layout.svelte`, NOT in `UpdateBadge.svelte` — so it
  //     keeps rendering after the badge closes its popover.
  //   - Progress data comes from `$orchestrator.progress` (already
  //     populated by the `install_progress` Tauri listener at
  //     `stores/orchestrator.ts:108`). We DO NOT subscribe to the channel
  //     ourselves — dual listeners would split the event stream and were
  //     called out as a footgun in the wiring note. Reading the
  //     reactive store is enough.
  //   - Progress bar reuses the `cgr-progress-fill` 4px-track style from
  //     `CodeGraphReanalysisModal.svelte:265-270` (teal 0.8 → 1.0 on
  //     complete, 0.2s ease width transition). Battle-tested look.
  //   - Above the bar, a "orbital-ring" orbital logo: dual counter-
  //     rotating rings + a pulsing core with a VECTOR robot monogram
  //     (inline SVG — crisp on HiDPI, unlike the old /logo.png raster).
  //     A conic-gradient arc behind the rings tracks the REAL `fillPct`
  //     (from install_progress events) so the only progress-coupled bit
  //     stays honest — it visualises the genuine percentage the bar also
  //     shows, never a fabricated one. On completed/failed the rings stop
  //     spinning and recolor (teal/pink). Replaces the earlier Fay-FAB
  //     clip-path fill (two stacked <img>) which used a soft raster logo.
  //
  // Lifecycle:
  //
  //   - On open (UpdateBadge.handleAction → ui.openOrchestratorUpdateProgress()),
  //     we read $updater.updating to drive the running animation +
  //     `$orchestrator.progress` for the bar fill.
  //   - When the Rust action resolves, $updater.updating flips to false.
  //     We hold the bar at 100% for COMPLETED_HOLD_MS, then fade out the
  //     overlay over FADE_OUT_MS, then auto-call ui.closeOrchestratorUpdateProgress().
  //     The held-then-fade rhythm mirrors the Fay FAB so muscle memory
  //     transfers across products.
  //   - On error, the modal stays visible with the error string until the
  //     user clicks "Dismiss" (then we close the flag + clear the updater
  //     error). No auto-close on error — the user needs to read it.
  //   - The `runRestart` flow (binary_stale) is special: the Rust side
  //     exits the process mid-call, so we never see resolve. The overlay
  //     blanks with the launcher. Correct UX.
  //
  // Cross-OS: pure DOM + CSS in the Tauri WebView, identical render on
  // Windows / macOS / Linux. The Rust side handles per-OS quirks
  // (Windows pre-pull binary rename, etc.) — none of those concerns
  // bleed up here.

  import { orchestrator } from '$lib/stores/orchestrator';
  import { updater } from '$lib/stores/updater';
  import { ui } from '$lib/stores/ui';

  const COMPLETED_HOLD_MS = 1800;
  const FADE_OUT_MS = 400;

  const orchState = $derived($orchestrator);
  const upd = $derived($updater);

  // Local phase: 'running' while the updater is in flight, 'completed'
  // for the hold+fade-out window after a successful resolve, 'failed'
  // when an error is set. Drives both the visual state and the auto-
  // close timer below.
  type Phase = 'running' | 'completed' | 'failed';
  let phase = $state<Phase>('running');
  let fadingOut = $state(false);

  // Hold/fade timers. Cleared in the rising-edge branch so a back-to-back
  // open doesn't fire stale callbacks against a fresh lifecycle.
  let holdTimer: ReturnType<typeof setTimeout> | null = null;
  let hideTimer: ReturnType<typeof setTimeout> | null = null;
  function clearTimers() {
    if (holdTimer !== null) { clearTimeout(holdTimer); holdTimer = null; }
    if (hideTimer !== null) { clearTimeout(hideTimer); hideTimer = null; }
  }

  // Last-seen `updating` value so we can detect the falling edge
  // (true → false). Svelte 5 $effect runs once per dependency tick;
  // the prev-value pattern is the canonical way to spot edges.
  let prevUpdating = $state(false);

  $effect(() => {
    const isUpdating = upd.updating;
    const hasError = !!upd.error;

    // Error trumps everything — show the failed state and stop the
    // auto-close timer. The user dismisses manually.
    if (hasError) {
      clearTimers();
      phase = 'failed';
      fadingOut = false;
      prevUpdating = isUpdating;
      return;
    }

    // Rising edge: updating just became true (or it's already true on
    // first run). Reset to running, kill any pending timers from a
    // previous lifecycle.
    if (isUpdating) {
      clearTimers();
      phase = 'running';
      fadingOut = false;
      prevUpdating = true;
      return;
    }

    // Falling edge: updating just became false. Enter completed →
    // hold COMPLETED_HOLD_MS → fade for FADE_OUT_MS → close.
    if (prevUpdating && !isUpdating) {
      clearTimers();
      phase = 'completed';
      fadingOut = false;
      holdTimer = setTimeout(() => { fadingOut = true; }, COMPLETED_HOLD_MS);
      hideTimer = setTimeout(() => {
        ui.closeOrchestratorUpdateProgress();
        // After the layout unmounts us, the bound state is irrelevant —
        // but reset anyway so a remount starts clean.
        phase = 'running';
        fadingOut = false;
      }, COMPLETED_HOLD_MS + FADE_OUT_MS);
      prevUpdating = false;
      return;
    }

    prevUpdating = isUpdating;
  });

  // Percentage 0–100, clamped, with the "force 100 when completed" rule
  // from the Fay FAB so the bar visually hits the end even if the last
  // backend event was 95% (e.g. restart fired before the 100% emit).
  const fillPct = $derived(
    phase === 'completed'
      ? 100
      : Math.max(0, Math.min(100, orchState.progress?.percentage ?? 0))
  );

  // Modal title depends on which updater action is in flight. We read
  // `upd.kind` while the popover was open — UpdateBadge resets it when
  // the user dismisses, but during an active update it remains the
  // value the user clicked on.
  const title = $derived.by(() => {
    switch (upd.kind) {
      case 'binary_stale':  return 'Restarting launcher';
      case 'install_stale': return 'Installing update';
      case 'remote_ahead':  return 'Updating orchestrator';
      default:              return 'Updating orchestrator';
    }
  });

  // Stage + message come straight from the install_progress events. We
  // surface both so power users see the short tag ("update", "install",
  // "restart") that maps to the installer.rs::emit_progress call sites,
  // AND the human-readable message ("Pulling latest changes…", "Applying
  // updates…").
  const stage = $derived(orchState.progress?.stage ?? '');
  const message = $derived.by(() => {
    if (phase === 'completed') return 'Update complete';
    if (phase === 'failed')    return upd.error ?? 'Update failed';
    return orchState.progress?.message ?? 'Working…';
  });

  // Dismiss the error state. Closes the overlay flag + clears the updater
  // error so subsequent updater interactions don't immediately re-show the
  // failed banner.
  function handleDismissError() {
    clearTimers();
    updater.clearError();
    ui.closeOrchestratorUpdateProgress();
  }

  // ARIA live region label — read by screen readers as the bar advances.
  const a11yLabel = $derived(
    phase === 'failed'
      ? `Update failed: ${upd.error ?? 'unknown error'}`
      : phase === 'completed'
        ? 'Update complete'
        : `Updating, ${Math.round(fillPct)} percent: ${message}`
  );
</script>

<!-- Full-screen blocking overlay. No close button on the running/completed
     path — the modal owns its lifecycle and closes itself when done. On
     error we show an explicit "Dismiss" button. -->
<div
  class="oup-overlay"
  class:fading-out={fadingOut}
  style="--fade-ms: {FADE_OUT_MS}ms;"
  role="dialog"
  aria-modal="true"
  aria-live="polite"
  aria-label={a11yLabel}
>
  <div class="oup-card">
    <!-- orbital-ring orbital logo. Dual counter-rotating rings + pulsing
         core with a VECTOR robot monogram (crisp on HiDPI, unlike the old
         /logo.png raster which looked soft). On `completed` the rings turn
         solid teal; on `failed` they turn pink. The conic ring underneath
         is the only progress-coupled bit: its sweep tracks the REAL fillPct
         (from install_progress events) so the animation never fakes data —
         it just visualises the genuine percentage the bar also shows. -->
    <div
      class="oup-logo-wrap"
      class:is-complete={phase === 'completed'}
      class:is-failed={phase === 'failed'}
    >
      <!-- progress-coupled conic sweep (real fillPct, not decorative) -->
      <div
        class="oup-progress-arc"
        style="background: conic-gradient(var(--arc-color) {fillPct * 3.6}deg, rgba(255,255,255,0.05) 0deg);"
        aria-hidden="true"
      ></div>
      <div class="oup-ring" aria-hidden="true"></div>
      <div class="oup-ring inner" aria-hidden="true"></div>
      <div class="oup-core" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <rect x="5" y="7" width="14" height="11" rx="3.2" stroke="currentColor" stroke-width="1.8"/>
          <line x1="12" y1="3.4" x2="12" y2="6.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          <circle cx="12" cy="2.6" r="1.2" fill="#ff4fa0"/>
          <circle cx="9.4" cy="12.2" r="1.5" fill="currentColor"/>
          <circle cx="14.6" cy="12.2" r="1.5" fill="#7b5fff"/>
        </svg>
      </div>
    </div>

    <!-- Title — which of the three updater actions is in flight. -->
    <h2 class="oup-title">{title}</h2>

    <!-- Stage tag (small uppercase teal). Only shown when we have one;
         the early ticks (~2%) may not have a stage string yet. -->
    {#if stage && phase === 'running'}
      <p class="oup-stage">{stage}</p>
    {/if}

    <!-- Progress bar — reuses Martino's blessed `cgr-progress-fill`
         pattern from CodeGraphReanalysisModal.svelte:178-185. 4px track,
         teal fill, 0.2s ease width transition; `.complete` class bumps
         the fill from 0.8 to 1.0 opacity. -->
    <div class="oup-progress-track" aria-hidden="true">
      <div
        class="oup-progress-fill"
        class:complete={phase === 'completed'}
        class:failed={phase === 'failed'}
        style:width="{phase === 'failed' ? 100 : fillPct}%"
      ></div>
    </div>

    <!-- Percentage + status message line. -->
    <p class="oup-progress-text">
      {#if phase === 'failed'}
        <span class="oup-pct-failed">FAILED</span>
        <span class="oup-message-failed">{message}</span>
      {:else}
        <span class="oup-pct">{Math.round(fillPct)}%</span>
        <span class="oup-message">{message}</span>
      {/if}
    </p>

    <!-- Error block + dismiss button (failed state only). -->
    {#if phase === 'failed'}
      <div class="oup-error-actions">
        <button class="oup-dismiss-btn" onclick={handleDismissError}>
          Dismiss
        </button>
      </div>
    {/if}

    <!-- Hint to keep hands off during the update. Hidden once we're in
         the completed/failed phase so it doesn't fight the action affordances. -->
    {#if phase === 'running'}
      <p class="oup-hint">
        Please don't close the launcher — it will restart automatically when the update finishes.
      </p>
    {/if}
  </div>
</div>

<style>
  /* Full-screen opaque blocking overlay. */
  .oup-overlay {
    position: fixed;
    inset: 0;
    z-index: 10000;
    background: rgba(5, 11, 31, 0.92);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: auto;
    animation: oup-overlay-in 200ms ease-out;
    transition: opacity var(--fade-ms, 400ms) ease-out;
  }
  .oup-overlay.fading-out { opacity: 0; }

  @keyframes oup-overlay-in {
    from { opacity: 0; }
    to   { opacity: 1; }
  }

  .oup-card {
    max-width: 480px;
    min-width: 340px;
    padding: 36px 32px 28px;
    background: rgba(13, 23, 53, 0.95);
    border: 1px solid rgba(0, 191, 166, 0.25);
    border-radius: 18px;
    box-shadow: 0 30px 80px rgba(0, 0, 0, 0.6);
    text-align: center;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
  }

  /* -------- orbital-ring orbital logo -------- */

  .oup-logo-wrap {
    position: relative;
    width: 128px;
    height: 128px;
    margin-bottom: 4px;
    /* per-phase accent color, consumed by rings + core monogram + arc */
    --arc-color: #00bfa6;
    color: #00bfa6;
    filter: drop-shadow(0 8px 24px rgba(0, 191, 166, 0.35));
  }
  .oup-logo-wrap.is-complete { --arc-color: #00bfa6; color: #00bfa6; }
  .oup-logo-wrap.is-failed {
    --arc-color: #ff4fa0;
    color: #ff4fa0;
    filter: drop-shadow(0 8px 24px rgba(255, 79, 160, 0.35));
  }

  /* Progress-coupled conic sweep — tracks REAL fillPct (set inline). The
     mask carves it into a thin ring so it reads as a progress arc, not a
     filled pie. width transition lives on the inline style update. */
  .oup-progress-arc {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 5px), #000 calc(100% - 4px));
    mask: radial-gradient(farthest-side, transparent calc(100% - 5px), #000 calc(100% - 4px));
    transition: background 0.25s ease;
    pointer-events: none;
  }

  .oup-ring {
    position: absolute;
    inset: 12px;
    border-radius: 50%;
    border: 2px solid rgba(123, 95, 255, 0.16);
    pointer-events: none;
  }
  .oup-ring::after {
    content: '';
    position: absolute;
    inset: -2px;
    border-radius: 50%;
    border: 2px solid transparent;
    border-top-color: currentColor;
    border-right-color: #7b5fff;
    animation: oup-spin 1.4s cubic-bezier(0.65, 0, 0.35, 1) infinite;
  }
  .oup-ring.inner { inset: 30px; border-color: rgba(0, 191, 166, 0.12); }
  .oup-ring.inner::after {
    border-top-color: #7b5fff;
    border-left-color: currentColor;
    border-right-color: transparent;
    animation: oup-spin-rev 2s linear infinite;
  }

  .oup-core {
    position: absolute;
    inset: 44px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 30%, #2b3370, #161a3a);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 0 24px rgba(0, 191, 166, 0.5), inset 0 0 12px rgba(0, 0, 0, 0.6);
    animation: oup-pulse 2.2s ease-in-out infinite;
    pointer-events: none;
  }
  .oup-core svg { width: 22px; height: 22px; display: block; }

  /* On the terminal phases the rings stop spinning (work is done/failed). */
  .oup-logo-wrap.is-complete .oup-ring::after,
  .oup-logo-wrap.is-failed .oup-ring::after,
  .oup-logo-wrap.is-complete .oup-ring.inner::after,
  .oup-logo-wrap.is-failed .oup-ring.inner::after {
    animation-play-state: paused;
    border-top-color: currentColor;
    border-right-color: currentColor;
    border-left-color: currentColor;
  }

  @keyframes oup-spin     { to { transform: rotate(360deg); } }
  @keyframes oup-spin-rev { to { transform: rotate(-360deg); } }
  @keyframes oup-pulse {
    0%, 100% { box-shadow: 0 0 18px rgba(0, 191, 166, 0.45), inset 0 0 12px rgba(0, 0, 0, 0.6); }
    50%      { box-shadow: 0 0 34px rgba(0, 191, 166, 0.6),  inset 0 0 12px rgba(0, 0, 0, 0.6); }
  }

  @media (prefers-reduced-motion: reduce) {
    .oup-ring::after, .oup-ring.inner::after, .oup-core { animation: none; }
  }

  /* -------- Title + stage -------- */

  .oup-title {
    margin: 0;
    font-size: 18px;
    font-weight: 700;
    color: var(--color-text, #F1F5F9);
  }
  .oup-stage {
    margin: 0;
    font-size: 11px;
    color: var(--color-teal, #00BFA6);
    text-transform: uppercase;
    letter-spacing: 1.5px;
    font-weight: 600;
  }

  /* -------- Progress bar (mirrors cgr-progress-fill) -------- */

  .oup-progress-track {
    width: 100%;
    height: 4px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 6px;
  }
  .oup-progress-fill {
    height: 100%;
    background: rgba(0, 191, 166, 0.8);
    transition: width 0.2s ease, background 0.3s ease;
  }
  .oup-progress-fill.complete { background: rgb(0, 191, 166); }
  .oup-progress-fill.failed   { background: rgba(255, 79, 160, 0.85); }

  .oup-progress-text {
    margin: 0;
    font-size: 12px;
    color: #ccc;
    font-family: ui-monospace, monospace;
    display: flex;
    gap: 8px;
    justify-content: center;
    align-items: baseline;
    flex-wrap: wrap;
    max-width: 380px;
  }
  .oup-pct {
    font-weight: 700;
    color: var(--color-teal, #00BFA6);
  }
  .oup-message {
    color: var(--color-mid, #94A3B8);
  }
  .oup-pct-failed {
    font-weight: 700;
    color: var(--color-pink, #FF4FA0);
    letter-spacing: 0.5px;
  }
  .oup-message-failed {
    color: var(--color-pink, #FF4FA0);
    word-break: break-word;
    text-align: left;
  }

  /* -------- Error actions -------- */

  .oup-error-actions {
    display: flex;
    justify-content: center;
    margin-top: 8px;
  }
  .oup-dismiss-btn {
    padding: 6px 14px;
    background: rgba(255, 79, 160, 0.15);
    border: 1px solid rgba(255, 79, 160, 0.4);
    border-radius: 8px;
    color: var(--color-pink, #FF4FA0);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s ease;
  }
  .oup-dismiss-btn:hover {
    background: rgba(255, 79, 160, 0.25);
  }

  /* -------- Hint -------- */

  .oup-hint {
    margin: 8px 0 0;
    font-size: 11px;
    color: var(--color-muted, #475569);
    font-style: italic;
    line-height: 1.4;
    max-width: 320px;
  }
</style>
