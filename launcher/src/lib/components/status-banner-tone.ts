// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.95 R7 — the ONE tone vocabulary for the launcher's status banners.
//
// Every banner used to carry its own `.status-<its own status string>` CSS
// block (four verbatim clones: KgSyncBanner, KgSummaryBanner,
// CodeGraphBuildBanner, OperationProgressBanner). The shell
// (`StatusBannerShell.svelte`) now owns the palette once, keyed on a small
// TONE union; each banner maps ITS domain status onto a tone here, where the
// mapping is unit-testable without mounting a component.
//
// Tones (values are the pre-extraction ones, verbatim — not a re-derivation;
// palette per `.claude/references/VCO_BRAND_REFERENCE.md`):
//   pending → neutral grey   — queued, nothing happening yet
//   running → teal #00BFA6   — work in flight (brand primary accent)
//   success → green          — finished cleanly
//   warning → amber          — INFORMATIONAL, never an alarm: skipped work,
//                              a deferral, a partial prune. Never offers the
//                              "something broke" red.
//   failed  → pink #FF4FA0   — a genuine failure; the only tone that gets
//                              role="alert" and a Retry.

import type {
  CodeGraphBuildStatus,
  KgSummaryStatus,
  KgSyncStatus,
  ProjectSetupStatus,
} from '$lib/types/launcher';

export type BannerTone = 'pending' | 'running' | 'success' | 'warning' | 'failed';

/** KG-sync banner/pill: `skipped` (no knowledge/ or docs/ content) is
 *  informational, not a failure. */
export function toneForKgSyncStatus(status: KgSyncStatus): BannerTone {
  switch (status) {
    case 'pending': return 'pending';
    case 'running': return 'running';
    case 'success': return 'success';
    case 'skipped': return 'warning';
    case 'failed': return 'failed';
  }
}

/** KG-summary banner: same lifecycle as KG sync. */
export function toneForKgSummaryStatus(status: KgSummaryStatus): BannerTone {
  switch (status) {
    case 'pending': return 'pending';
    case 'running': return 'running';
    case 'success': return 'success';
    case 'skipped': return 'warning';
    case 'failed': return 'failed';
  }
}

/** Code-graph build: `partial` means inserts SUCCEEDED and only the stale-row
 *  prune was incomplete (v0.2.73 C-11) — amber, not the failure pink. */
export function toneForCodeGraphBuildStatus(status: CodeGraphBuildStatus): BannerTone {
  switch (status) {
    case 'pending': return 'pending';
    case 'running': return 'running';
    case 'success': return 'success';
    case 'partial': return 'warning';
    case 'skipped': return 'warning';
    case 'failed': return 'failed';
  }
}

/** Project setup (add/adopt): `deferred` is informational — the project is
 *  usable, the deferred work catches up on its own — so amber, no Retry.
 *  `pending` (queued behind another add) presents as running. */
export function toneForProjectSetupStatus(status: ProjectSetupStatus): BannerTone {
  switch (status) {
    case 'pending': return 'running';
    case 'running': return 'running';
    case 'done': return 'success';
    case 'deferred': return 'warning';
    case 'failed': return 'failed';
  }
}
