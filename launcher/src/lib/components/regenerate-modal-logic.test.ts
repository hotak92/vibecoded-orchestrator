// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.71 Track T-C-modal — tests for the model-switch "keep previous model"
// decision logic + the invoke contract the modal uses to make the choice
// sticky (set_project_active_embedding, source=user).

import { describe, it, expect, vi } from 'vitest';
import {
  pickKeepCandidate,
  keepCandidateDisplayCount,
  showsModelSwitchPanel,
  offersKeepPrevious,
  type ModelSwitchContext,
  type SlotPopulatedCount,
} from './regenerate-modal-logic';

function ctx(
  partial: Partial<ModelSwitchContext> & { slotCounts: SlotPopulatedCount[] },
): ModelSwitchContext {
  return {
    newProfile: 'arctic',
    mostPopulatedProfile: null,
    total: 0,
    ...partial,
  };
}

describe('pickKeepCandidate (smart default)', () => {
  it('returns null when there is no model switch', () => {
    expect(pickKeepCandidate(null)).toBeNull();
  });

  it('returns null when no slots are populated', () => {
    expect(pickKeepCandidate(ctx({ slotCounts: [] }))).toBeNull();
  });

  it('prefers the most-populated-profile from the probe', () => {
    const c = ctx({
      mostPopulatedProfile: 'qwen3',
      total: 100,
      slotCounts: [
        { slot: 'qwen3_embed', profile: 'qwen3', populated: 100 },
        { slot: 'arctic2_embed', profile: 'arctic', populated: 30 },
      ],
    });
    expect(pickKeepCandidate(c)?.profile).toBe('qwen3');
    expect(pickKeepCandidate(c)?.populated).toBe(100);
  });

  it('falls back to the highest-populated slot when smart default is absent', () => {
    const c = ctx({
      mostPopulatedProfile: null,
      total: 50,
      slotCounts: [
        { slot: 'qwen3_embed', profile: 'qwen3', populated: 10 },
        { slot: 'arctic2_embed', profile: 'arctic', populated: 40 },
      ],
    });
    expect(pickKeepCandidate(c)?.profile).toBe('arctic');
  });

  it('falls back to highest when smart default profile is not in the slot set', () => {
    const c = ctx({
      mostPopulatedProfile: 'openai', // not present below
      total: 50,
      slotCounts: [
        { slot: 'qwen3_embed', profile: 'qwen3', populated: 35 },
        { slot: 'arctic2_embed', profile: 'arctic', populated: 15 },
      ],
    });
    expect(pickKeepCandidate(c)?.profile).toBe('qwen3');
  });
});

describe('keepCandidateDisplayCount (L2: profile total across slots)', () => {
  it('returns 0 when there is no model switch', () => {
    expect(keepCandidateDisplayCount(null)).toBe(0);
  });

  it('returns 0 when no slots are populated', () => {
    expect(keepCandidateDisplayCount(ctx({ slotCounts: [] }))).toBe(0);
  });

  it('returns the single slot count when a profile spans one slot', () => {
    const c = ctx({
      mostPopulatedProfile: 'qwen3',
      total: 100,
      slotCounts: [
        { slot: 'qwen3_embed', profile: 'qwen3', populated: 100 },
        { slot: 'arctic2_embed', profile: 'arctic', populated: 30 },
      ],
    });
    expect(keepCandidateDisplayCount(c)).toBe(100);
  });

  it('SUMS across every slot mapping to the candidate profile (the L2 bug)', () => {
    // `arctic` spans two slots (legacy ollama_embed + current arctic2_embed).
    // pickKeepCandidate returns only the first matching slot's count (40);
    // the DISPLAY number must be the profile total (40 + 25 = 65).
    const c = ctx({
      mostPopulatedProfile: 'arctic',
      total: 100,
      slotCounts: [
        { slot: 'ollama_embed', profile: 'arctic', populated: 40 },
        { slot: 'arctic2_embed', profile: 'arctic', populated: 25 },
        { slot: 'qwen3_embed', profile: 'qwen3', populated: 10 },
      ],
    });
    // Sanity: the candidate slot's own count understates the profile total.
    expect(pickKeepCandidate(c)?.populated).toBe(40);
    // The display count is the SUM across both arctic slots.
    expect(keepCandidateDisplayCount(c)).toBe(65);
  });

  it('matches the fallback candidate profile when no smart default is given', () => {
    // No mostPopulatedProfile → pickKeepCandidate falls back to the highest
    // single slot (arctic2_embed=40 → profile arctic); display sums arctic.
    const c = ctx({
      mostPopulatedProfile: null,
      total: 60,
      slotCounts: [
        { slot: 'qwen3_embed', profile: 'qwen3', populated: 5 },
        { slot: 'arctic2_embed', profile: 'arctic', populated: 40 },
        { slot: 'ollama_embed', profile: 'arctic', populated: 15 },
      ],
    });
    expect(pickKeepCandidate(c)?.profile).toBe('arctic');
    expect(keepCandidateDisplayCount(c)).toBe(55);
  });
});

describe('panel + button visibility', () => {
  it('shows the model-switch panel iff a switch context is present', () => {
    expect(showsModelSwitchPanel(null)).toBe(false);
    expect(showsModelSwitchPanel(ctx({ slotCounts: [] }))).toBe(true);
  });

  it('offers "keep previous" only when there is a populated slot to revert to', () => {
    expect(offersKeepPrevious(ctx({ slotCounts: [] }))).toBe(false);
    expect(
      offersKeepPrevious(
        ctx({
          slotCounts: [
            { slot: 'qwen3_embed', profile: 'qwen3', populated: 5 },
          ],
        }),
      ),
    ).toBe(true);
  });
});

describe('keep-previous invoke contract (sticky, source=user)', () => {
  // Mirror the modal's keepPreviousModel() invoke shape: it calls
  // `set_project_active_embedding` with { projectId, profile } — the T-B-emb
  // command that records a deliberate user pick (source=user). We assert the
  // command name + that the profile passed is the smart-default candidate.
  async function simulateKeepPrevious(
    invoke: (cmd: string, args: Record<string, unknown>) => Promise<unknown>,
    projectId: string,
    modelSwitch: ModelSwitchContext,
  ): Promise<string | null> {
    const candidate = pickKeepCandidate(modelSwitch);
    if (!candidate) return null;
    await invoke('set_project_active_embedding', {
      projectId,
      profile: candidate.profile,
    });
    return candidate.profile;
  }

  it('invokes set_project_active_embedding with the most-populated profile', async () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    const c = ctx({
      mostPopulatedProfile: 'qwen3',
      total: 100,
      slotCounts: [
        { slot: 'qwen3_embed', profile: 'qwen3', populated: 100 },
        { slot: 'arctic2_embed', profile: 'arctic', populated: 30 },
      ],
    });
    const chosen = await simulateKeepPrevious(invoke, 'proj-1', c);
    expect(chosen).toBe('qwen3');
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith('set_project_active_embedding', {
      projectId: 'proj-1',
      profile: 'qwen3',
    });
  });

  it('does not invoke when there is no candidate to keep', async () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    const chosen = await simulateKeepPrevious(
      invoke,
      'proj-1',
      ctx({ slotCounts: [] }),
    );
    expect(chosen).toBeNull();
    expect(invoke).not.toHaveBeenCalled();
  });
});
