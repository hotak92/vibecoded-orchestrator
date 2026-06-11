<!--
  V52-AH-FE (v0.2.53 Track E, reworked v0.2.54 Track C FE C-1) — FE
  consumer for the V52-AH boot recovery report.

  v0.2.54 delivery model — PULL is canonical, events are belt-and-braces:

    The Rust boot probe (`poll_update_lock_on_boot` in
    `launcher/src-tauri/src/lib.rs::setup`) runs BEFORE the webview
    loads. Tauri does not buffer events, so the `vct-update-recovered` /
    `vct-update-failed` emits from `setup` were structurally
    unreceivable (no listener exists yet — the v0.2.53 toast never
    fired in production). The probe result is now cached in app state
    (`UpdateRecoveryCache`); this component PULLS it on mount via
    `get_update_recovery_report`, which has one-shot `take()` semantics
    so a layout remount can never double-toast.

    The event listeners are kept as belt-and-braces for any future
    re-emit surface; `consumed` guards against the theoretical
    double-delivery (event + pull).

  Backend payload shape (mirrors `UpdateRecoveryReport`):

    {
      recovered: bool,           // swap succeeded (authoritative
                                 // update.result.json from vct-updater)
      stale_or_invalid: bool,    // swap failed / updater crashed
      lock_path: string | null,
      reason: string | null,
    }

  Toast store auto-dismisses after 4s; this component renders nothing —
  it is a pure side-effect mount calling `toast.success(...)` /
  `toast.error(...)` on the toast root mounted in `+layout.svelte`.

  POSIX hosts effectively no-op: the V52-AH backend probe is a Windows
  feature; on Linux/macOS the lock/result files are never written, so
  the pull returns the empty default and neither event fires.
  Browser-mode (no Tauri runtime) also no-ops (`invoke` throws → caught;
  `listen()` short-circuits).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { listen, isTauriRuntime, invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import {
    handleReport,
    type UpdateRecoveryPayload,
  } from '$lib/update-toast-handlers';

  // Lazy-load the version at use time so we don't pay the cost in
  // browser mode (no Tauri runtime). Fallback empty string for the
  // edge case where the API resolves but returns nothing — the
  // handler downstream renders a generic message in that case.
  async function getAppVersion(): Promise<string> {
    if (!isTauriRuntime()) return '';
    try {
      const { getVersion } = await import('@tauri-apps/api/app');
      return (await getVersion()) || '';
    } catch {
      return '';
    }
  }

  // Once-only guard across both delivery paths (pull + events). The
  // backend cache is itself one-shot, so this is a second line of
  // defense, not the primary mechanism.
  let consumed = false;

  async function deliver(payload: UpdateRecoveryPayload | null | undefined) {
    if (consumed || !payload) return;
    const version = await getAppVersion();
    if (handleReport(payload, version, toast)) {
      consumed = true;
      console.debug('[update-toast] delivered', {
        recovered: payload.recovered,
        lock_path: payload.lock_path,
        reason: payload.reason,
        version,
      });
    }
  }

  onMount(() => {
    let unlistenRecovered: (() => void) | undefined;
    let unlistenFailed: (() => void) | undefined;

    // Canonical path (FE C-1): pull the cached boot report. One-shot on
    // the backend (`take()`), so remounts get the empty default.
    (async () => {
      if (!isTauriRuntime()) return;
      try {
        const report = await invoke<UpdateRecoveryPayload>(
          'get_update_recovery_report',
        );
        await deliver(report);
      } catch (e) {
        console.debug(
          '[update-toast] get_update_recovery_report pull failed:',
          e,
        );
      }
    })();

    // Belt-and-braces listeners. Each `listen()` returns a no-op
    // cleanup when not in a Tauri runtime, so the awaits are cheap in
    // browser mode.
    (async () => {
      try {
        unlistenRecovered = await listen<UpdateRecoveryPayload>(
          'vct-update-recovered',
          (e) => void deliver(e.payload),
        );
      } catch (e) {
        console.debug(
          '[update-toast] vct-update-recovered subscribe failed (browser mode?):',
          e,
        );
      }
    })();

    (async () => {
      try {
        unlistenFailed = await listen<UpdateRecoveryPayload>(
          'vct-update-failed',
          (e) => void deliver(e.payload),
        );
      } catch (e) {
        console.debug(
          '[update-toast] vct-update-failed subscribe failed (browser mode?):',
          e,
        );
      }
    })();

    return () => {
      if (unlistenRecovered) unlistenRecovered();
      if (unlistenFailed) unlistenFailed();
    };
  });
</script>

<!--
  This component renders nothing — it is a pure side-effect mount.
  The toast UI is rendered by `Toast.svelte` (mounted alongside us in
  `+layout.svelte`) which subscribes to the toast store.
-->
