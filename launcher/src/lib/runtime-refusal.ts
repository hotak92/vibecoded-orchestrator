// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.97 review R11 L6 — why a stale runtime record was NOT switched, as
// the install preflight modal shows it.
//
// `check_container_runtime_available` (install_preflight.rs) carries
// `not_switched`: the reason the stale-record reconcile declined, in the
// wording every VCO surface shares (`vco_lib/runtime_reconcile_messages.toml`).
// Before R11 the launcher only logged it, so the modal said "docker is
// pinned but not usable" without saying why VCO would not use podman.

/** The slice of `RuntimeAvailability` this decision reads. */
export interface NotSwitchedSource {
  pinned_unusable?: boolean;
  not_switched?: string | null;
}

/**
 * The reason to show, or `null`: only on the refused-pin path, and only
 * when the backend gave one (a blank string is no reason).
 */
export function notSwitchedReason(av: NotSwitchedSource | null | undefined): string | null {
  if (!av || av.pinned_unusable !== true) return null;
  const why = (av.not_switched ?? '').trim();
  return why === '' ? null : why;
}
