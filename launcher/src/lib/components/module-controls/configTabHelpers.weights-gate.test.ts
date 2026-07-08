// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.75 RL-11 — the "Download default weights" manifest button is hidden
// for ALL tiers until the bucket-populated probe positively confirms the
// server can serve a bundle. The bucket is PARKED-empty today (three
// prerequisites pending), so an unconfirmed button is a guaranteed dead
// click. Tier mapping:
//   * free tier   → the Rust probe short-circuits to `false` without a
//     license key → hidden (pre-RL-11 behaviour preserved).
//   * Pro, probe false/pending → hidden.
//   * Pro, probe true          → visible.

import { describe, expect, it } from 'vitest';
import type { ActionRef, ConfigControl } from '$lib/types/manifest';
import {
  actionReferencesCommand,
  buttonHiddenByWeightsProbe,
  configTabHasDefaultWeightsButton,
  DOWNLOAD_DEFAULT_WEIGHTS_COMMAND,
} from './configTabHelpers';

// ── fixtures (shapes mirror the RL manifest's config_tab) ───────────────

const downloadAction: ActionRef = {
  kind: 'tauri_command',
  command: DOWNLOAD_DEFAULT_WEIGHTS_COMMAND,
  args: { module_id: 'vct-rl-reranker' },
};

const chainedDownloadAction = {
  kind: 'chained_action',
  steps: [
    {
      kind: 'tauri_command',
      command: DOWNLOAD_DEFAULT_WEIGHTS_COMMAND,
      args: {},
    },
    { kind: 'http', method: 'POST', path: '/finetune' },
  ],
} as unknown as ActionRef;

const unrelatedAction = {
  kind: 'http',
  method: 'POST',
  path: '/train_global',
} as unknown as ActionRef;

const downloadButton = {
  kind: 'button',
  id: 'download_default_weights',
  label: 'Download default weights',
  action: downloadAction,
} as ConfigControl;

const chainedDownloadButton = {
  kind: 'button',
  id: 'download_and_train',
  label: 'Download default + offline pass on top',
  action: chainedDownloadAction,
} as ConfigControl;

const unrelatedButton = {
  kind: 'button',
  id: 'retrain_global',
  label: 'Retrain global model',
  action: unrelatedAction,
} as ConfigControl;

const legacyStringButton = {
  kind: 'button',
  id: 'legacy_download',
  label: 'Legacy download',
  action: DOWNLOAD_DEFAULT_WEIGHTS_COMMAND,
} as ConfigControl;

// ── actionReferencesCommand ──────────────────────────────────────────────

describe('actionReferencesCommand', () => {
  it('matches a tauri_command descriptor', () => {
    expect(
      actionReferencesCommand(downloadAction, DOWNLOAD_DEFAULT_WEIGHTS_COMMAND),
    ).toBe(true);
  });

  it('matches inside chained_action steps (the download+train button)', () => {
    expect(
      actionReferencesCommand(
        chainedDownloadAction,
        DOWNLOAD_DEFAULT_WEIGHTS_COMMAND,
      ),
    ).toBe(true);
  });

  it('matches the legacy string form', () => {
    expect(
      actionReferencesCommand(
        DOWNLOAD_DEFAULT_WEIGHTS_COMMAND,
        DOWNLOAD_DEFAULT_WEIGHTS_COMMAND,
      ),
    ).toBe(true);
  });

  it('does not match unrelated http actions', () => {
    expect(
      actionReferencesCommand(unrelatedAction, DOWNLOAD_DEFAULT_WEIGHTS_COMMAND),
    ).toBe(false);
  });
});

// ── buttonHiddenByWeightsProbe ───────────────────────────────────────────

describe('buttonHiddenByWeightsProbe', () => {
  it('hides the download button for Pro when the probe says false', () => {
    expect(buttonHiddenByWeightsProbe(downloadButton, false)).toBe(true);
  });

  it('hides while the probe is pending (null) — parked bucket default', () => {
    expect(buttonHiddenByWeightsProbe(downloadButton, null)).toBe(true);
  });

  it('shows the button when the probe confirms the bucket (true)', () => {
    expect(buttonHiddenByWeightsProbe(downloadButton, true)).toBe(false);
  });

  it('gates the chained download+train button identically', () => {
    expect(buttonHiddenByWeightsProbe(chainedDownloadButton, false)).toBe(true);
    expect(buttonHiddenByWeightsProbe(chainedDownloadButton, true)).toBe(false);
  });

  it('free tier stays hidden regardless (probe is false without a key)', () => {
    // The Rust probe returns false when no license key is configured —
    // the helper's job is to render that verdict as hidden.
    expect(buttonHiddenByWeightsProbe(downloadButton, false)).toBe(true);
    expect(buttonHiddenByWeightsProbe(legacyStringButton, false)).toBe(true);
  });

  it('never hides unrelated buttons, whatever the probe says', () => {
    expect(buttonHiddenByWeightsProbe(unrelatedButton, false)).toBe(false);
    expect(buttonHiddenByWeightsProbe(unrelatedButton, null)).toBe(false);
    expect(buttonHiddenByWeightsProbe(unrelatedButton, true)).toBe(false);
  });

  it('never applies to non-button controls', () => {
    const checkbox = {
      kind: 'checkbox',
      id: 'x',
      label: 'X',
    } as ConfigControl;
    expect(buttonHiddenByWeightsProbe(checkbox, false)).toBe(false);
  });
});

// ── configTabHasDefaultWeightsButton (probe-invocation gate) ────────────

describe('configTabHasDefaultWeightsButton', () => {
  it('true when any section carries a gated button', () => {
    const sections = [
      { title: 'A', controls: [unrelatedButton] },
      { title: 'B', controls: [downloadButton] },
    ] as never;
    expect(configTabHasDefaultWeightsButton(sections)).toBe(true);
  });

  it('false for tabs with no gated button — no edge round-trip', () => {
    const sections = [{ title: 'A', controls: [unrelatedButton] }] as never;
    expect(configTabHasDefaultWeightsButton(sections)).toBe(false);
  });
});
