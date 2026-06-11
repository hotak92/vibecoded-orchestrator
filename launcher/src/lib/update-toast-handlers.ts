// SPDX-License-Identifier: AGPL-3.0-or-later
//
// V52-AH-FE (v0.2.53 Track E) — pure-function handlers for the
// `vct-update-recovered` / `vct-update-failed` Tauri events that the
// V52-AH backend boot probe emits. Extracted from `UpdateToast.svelte`
// so they're unit-testable without standing up a DOM (the launcher's
// vitest config is pure-Node by design).
//
// The Svelte component (`UpdateToast.svelte`) is a thin shell that
// subscribes to the Tauri events + calls these handlers with the
// payload + a toast interface. All meaningful logic — message
// composition, version fallback, dedup keys, console breadcrumbs —
// lives here.

/**
 * Payload shape mirrored from
 * `launcher/src-tauri/src/commands/update_handoff.rs::UpdateRecoveryReport`.
 *
 * `lock_path` and `reason` are nullable in serde JSON output (snake_case);
 * the FE deserialiser preserves the snake_case keys verbatim.
 */
export interface UpdateRecoveryPayload {
  recovered: boolean;
  stale_or_invalid: boolean;
  lock_path: string | null;
  reason: string | null;
}

/**
 * Minimal toast surface needed by the handlers. Mirrors the public API
 * of `$lib/stores/toast::toast` without coupling to it (so the tests
 * can inject a stub).
 */
export interface ToastSurface {
  success(message: string, opts?: { key?: string }): void;
  error(message: string, opts?: { key?: string }): void;
}

/**
 * Build the message shown for `vct-update-recovered`. Falls back to a
 * generic phrase when the app version can't be resolved (Tauri API
 * unavailable, returned empty string, etc.).
 */
export function buildRecoveredMessage(version: string): string {
  const trimmed = version.trim();
  if (!trimmed) return 'Launcher update completed.';
  // Tolerate both 'v0.2.53' and '0.2.53' from the caller — strip a
  // leading 'v' if present so the final string doesn't double-prefix.
  const v = trimmed.startsWith('v') ? trimmed.slice(1) : trimmed;
  return `Launcher updated to v${v}.`;
}

/**
 * Build the message shown for `vct-update-failed`. Mentions the
 * specific rejection reason + lock path so the user knows where to
 * look. Tolerates missing fields per the backend's nullable schema.
 */
export function buildFailedMessage(
  reason: string | null,
  lockPath: string | null,
): string {
  const r = reason ?? 'unknown';
  const p = lockPath ?? '(unknown path)';
  return (
    `Update may have failed (${r}). Check ${p} ` +
    `and the launcher's update.log for details.`
  );
}

/**
 * Apply the recovery payload to the toast surface. Pure function with
 * no I/O — the caller is responsible for fetching the app version (via
 * Tauri's `getVersion()` or whatever future source) and forwarding it
 * here, and for owning the actual toast store.
 */
export function handleRecovered(
  payload: UpdateRecoveryPayload,
  version: string,
  toast: ToastSurface,
): void {
  toast.success(buildRecoveredMessage(version), {
    key: 'vct-update-recovered',
  });
}

/**
 * Apply the failure payload to the toast surface. Same separation as
 * `handleRecovered` — no I/O, no global state, fully testable.
 */
export function handleFailed(
  payload: UpdateRecoveryPayload,
  toast: ToastSurface,
): void {
  toast.error(buildFailedMessage(payload.reason, payload.lock_path), {
    key: 'vct-update-failed',
  });
}

/**
 * v0.2.54 Track C (FE C-1): single router used by BOTH delivery paths
 * (the pull-on-mount `get_update_recovery_report` invoke AND the
 * legacy Tauri event listeners). Returns `true` when the payload was
 * meaningful and a toast was raised — callers use the return value to
 * implement once-only dedup (the backend cache is one-shot too, so a
 * double toast would require the event AND the pull to both deliver;
 * the boolean makes the dedup explicit anyway).
 *
 * Empty/default payloads (`recovered=false`, `stale_or_invalid=false`)
 * are a no-op — that is what `get_update_recovery_report` returns on
 * every call after the first consume.
 */
export function handleReport(
  payload: UpdateRecoveryPayload | null | undefined,
  version: string,
  toast: ToastSurface,
): boolean {
  if (!payload) return false;
  if (payload.recovered) {
    handleRecovered(payload, version, toast);
    return true;
  }
  if (payload.stale_or_invalid) {
    handleFailed(payload, toast);
    return true;
  }
  return false;
}
