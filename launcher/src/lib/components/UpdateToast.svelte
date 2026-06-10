<!--
  V52-AH-FE (v0.2.53 Track E) — FE consumer for V52-AH backend events.

  Subscribes to two Tauri events fired ONCE per launcher boot by the
  `poll_update_lock_on_boot` probe in `launcher/src-tauri/src/lib.rs`:

    * `vct-update-recovered` — Windows stage1 updater completed the
      binary swap successfully and we are the newly-relaunched binary.
      Render a one-shot "Updated to v0.2.X" success toast.

    * `vct-update-failed` — A lock file was found but was stale or
      malformed, meaning the updater crashed mid-swap. Render a "may
      have failed" diagnostic toast with the path to `update.log` and
      the rejection reason for the user to triage.

  Backend payload shape (mirrors `UpdateRecoveryReport`):

    {
      recovered: bool,
      stale_or_invalid: bool,
      lock_path: string | null,
      reason: string | null,
    }

  Toast store auto-dismisses after 4s; this component does NOT need to
  manage its own visibility lifecycle. The component renders nothing on
  its own — it is a pure event-listener wrapper that calls
  `toast.success(...)` / `toast.error(...)` on the existing toast root
  (mounted in `+layout.svelte`).

  POSIX hosts effectively no-op: the V52-AH backend probe is a Windows
  feature; on Linux/macOS the lock file is never written, so neither
  event ever fires. The component subscribes regardless (cheap to do
  so + future cross-OS expansion stays trivial). Browser-mode (no
  Tauri runtime) also no-ops because `listen()` short-circuits.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { listen, isTauriRuntime } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import {
    handleRecovered,
    handleFailed,
    type UpdateRecoveryPayload,
  } from '$lib/update-toast-handlers';

  // Lazy-load the version at subscribe time so we don't pay the cost
  // in browser mode (no Tauri runtime). Fallback empty string for the
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

  onMount(() => {
    let unlistenRecovered: (() => void) | undefined;
    let unlistenFailed: (() => void) | undefined;

    // Subscribe asynchronously so a failure in either listener doesn't
    // block the other. Each call to `listen()` returns a no-op cleanup
    // when not in a Tauri runtime, so the awaits are cheap in browser
    // mode.
    (async () => {
      try {
        unlistenRecovered = await listen<UpdateRecoveryPayload>(
          'vct-update-recovered',
          async (e) => {
            const version = await getAppVersion();
            handleRecovered(e.payload, version, toast);
            // Debug breadcrumb — useful when triaging "did the update
            // really land?" from telemetry.
            console.debug('[update-toast] recovered', {
              lock_path: e.payload.lock_path,
              version,
            });
          },
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
          (e) => {
            handleFailed(e.payload, toast);
            console.warn('[update-toast] failed', {
              lock_path: e.payload.lock_path,
              reason: e.payload.reason,
            });
          },
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
